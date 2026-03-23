"""Data retention policies — prune old sessions, audit logs, and incident memory.

Runs as a background task (like the escalation timer). Configurable via env vars.

Default retention:
  - Resolved sessions:  30 days
  - Audit log entries:  90 days
  - Incident memory:   365 days (embeddings are valuable long-term)
  - In-memory sessions: evict resolved sessions after 1 hour
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Retention periods (days). 0 = never delete.
SESSION_RETENTION_DAYS = int(os.getenv("SESSION_RETENTION_DAYS", "30"))
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "90"))
MEMORY_RETENTION_DAYS = int(os.getenv("MEMORY_RETENTION_DAYS", "365"))
IN_MEMORY_EVICTION_HOURS = int(os.getenv("IN_MEMORY_EVICTION_HOURS", "1"))

# How often to run the cleanup (seconds)
CLEANUP_INTERVAL = int(os.getenv("RETENTION_CLEANUP_INTERVAL", str(3600)))  # 1 hour


class RetentionManager:
    """Background task that prunes old data from PostgreSQL and in-memory stores."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def run(self) -> None:
        """Main loop — runs forever, cleans up periodically."""
        try:
            while True:
                await asyncio.sleep(CLEANUP_INTERVAL)
                await self.cleanup()
        except asyncio.CancelledError:
            logger.info("Retention manager stopped")

    async def cleanup(self) -> dict[str, int]:
        """Run all cleanup tasks. Returns counts of items cleaned."""
        results: dict[str, int] = {}

        # 1. Evict resolved sessions from in-memory store
        results["in_memory_evicted"] = self._evict_in_memory_sessions()

        # 2. Prune old sessions from PostgreSQL
        results["pg_sessions_pruned"] = await self._prune_pg_sessions()

        # 3. Prune old audit log entries
        results["pg_audit_pruned"] = await self._prune_pg_audit()

        # 4. Prune old incident memory records
        results["pg_memory_pruned"] = await self._prune_pg_memory()

        total = sum(results.values())
        if total > 0:
            logger.info("Retention cleanup: %s", results)

        return results

    def _evict_in_memory_sessions(self) -> int:
        """Remove resolved/failed/escalated sessions older than IN_MEMORY_EVICTION_HOURS."""
        from .session import SessionPhase, session_store

        if IN_MEMORY_EVICTION_HOURS <= 0:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(hours=IN_MEMORY_EVICTION_HOURS)
        terminal_phases = {SessionPhase.RESOLVED, SessionPhase.FAILED, SessionPhase.ESCALATED}

        to_evict = [
            sid for sid, session in session_store._sessions.items()
            if session.phase in terminal_phases and session.updated_at < cutoff
        ]

        for sid in to_evict:
            del session_store._sessions[sid]

        return len(to_evict)

    async def _prune_pg_sessions(self) -> int:
        """Delete resolved sessions older than SESSION_RETENTION_DAYS from PostgreSQL."""
        if SESSION_RETENTION_DAYS <= 0:
            return 0

        from ..db import get_pool

        pool = get_pool()
        if not pool:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=SESSION_RETENTION_DAYS)
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM sessions WHERE resolved = TRUE AND updated_at < $1",
                    cutoff,
                )
                # result is like "DELETE 5"
                count = int(result.split()[-1]) if result else 0
                return count
        except Exception:
            logger.exception("Failed to prune old sessions")
            return 0

    async def _prune_pg_audit(self) -> int:
        """Delete audit log entries older than AUDIT_RETENTION_DAYS."""
        if AUDIT_RETENTION_DAYS <= 0:
            return 0

        from ..db import get_pool

        pool = get_pool()
        if not pool:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS)
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM audit_log WHERE created_at < $1",
                    cutoff,
                )
                count = int(result.split()[-1]) if result else 0
                return count
        except Exception:
            logger.exception("Failed to prune old audit logs")
            return 0

    async def _prune_pg_memory(self) -> int:
        """Delete incident memory records older than MEMORY_RETENTION_DAYS."""
        if MEMORY_RETENTION_DAYS <= 0:
            return 0

        from ..db import get_pool

        pool = get_pool()
        if not pool:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=MEMORY_RETENTION_DAYS)
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM incident_memory WHERE resolved_at < $1",
                    cutoff,
                )
                count = int(result.split()[-1]) if result else 0
                return count
        except Exception:
            logger.exception("Failed to prune old incident memory")
            return 0


# Module-level singleton
retention_manager = RetentionManager()
