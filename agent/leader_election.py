"""Leader election using PostgreSQL advisory locks.

Only one instance of the agent should run the escalation timer and
background tasks. This module uses pg_try_advisory_lock to elect a leader.

Usage:
    leader = PgLeaderElection(lock_id=1)
    await leader.start()

    if leader.is_leader:
        # Run escalation timer, hot-reload, etc.
        ...

    # The lock is automatically renewed every heartbeat_interval seconds.
    # If the leader crashes, the lock is released when the PG connection drops.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Stable advisory lock IDs for each background task
LOCK_ESCALATION_TIMER = 7890001
LOCK_RUNBOOK_WATCHER = 7890002
LOCK_SECRET_RELOADER = 7890003


class PgLeaderElection:
    """PostgreSQL advisory-lock-based leader election.

    Each instance tries to acquire a PG advisory lock at startup.
    Only one can hold the lock at a time (across all replicas).
    If the leader dies, PG auto-releases the session-level lock.
    """

    def __init__(
        self,
        lock_id: int = LOCK_ESCALATION_TIMER,
        heartbeat_interval: int = 15,
    ) -> None:
        self.lock_id = lock_id
        self.heartbeat_interval = heartbeat_interval
        self._is_leader = False
        self._task: asyncio.Task | None = None
        self._identity = f"{os.getenv('HOSTNAME', 'local')}-{os.getpid()}"
        self.leader_since: datetime | None = None

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def start(self) -> None:
        """Attempt to acquire leadership. Non-blocking."""
        await self._try_acquire()

    async def run(self) -> None:
        """Background loop: periodically re-check leadership."""
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                await self._try_acquire()
        except asyncio.CancelledError:
            await self._release()

    async def _try_acquire(self) -> None:
        """Try to acquire the advisory lock."""
        from ..db import get_pool

        pool = get_pool()
        if not pool:
            # No PG = single-instance mode, always leader
            if not self._is_leader:
                self._is_leader = True
                self.leader_since = datetime.now(timezone.utc)
                logger.info("Leader election: no PG — assuming leader (single-instance)")
            return

        try:
            async with pool.acquire() as conn:
                acquired = await conn.fetchval(
                    "SELECT pg_try_advisory_lock($1)", self.lock_id
                )

            if acquired and not self._is_leader:
                self._is_leader = True
                self.leader_since = datetime.now(timezone.utc)
                logger.info(
                    "Leader election: ACQUIRED lock %d (identity=%s)",
                    self.lock_id, self._identity,
                )
            elif not acquired and self._is_leader:
                self._is_leader = False
                self.leader_since = None
                logger.warning(
                    "Leader election: LOST lock %d (identity=%s)",
                    self.lock_id, self._identity,
                )
        except Exception:
            logger.exception("Leader election: failed to check lock %d", self.lock_id)

    async def _release(self) -> None:
        """Release the advisory lock on shutdown."""
        if not self._is_leader:
            return

        from ..db import get_pool

        pool = get_pool()
        if pool:
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1)", self.lock_id
                    )
                logger.info("Leader election: released lock %d", self.lock_id)
            except Exception:
                logger.exception("Leader election: failed to release lock %d", self.lock_id)

        self._is_leader = False

    def status(self) -> dict:
        return {
            "is_leader": self._is_leader,
            "lock_id": self.lock_id,
            "identity": self._identity,
            "leader_since": self.leader_since.isoformat() if self.leader_since else None,
        }
