"""Escalation timer — auto-escalate if no human responds within SLA.

Tracks sessions awaiting approval and fires escalation actions when the
configured time limit is breached.

SLA tiers (configurable via env):
  - critical:  5 minutes
  - warning:  15 minutes
  - info:     60 minutes

Actions on breach:
  1. Post urgent reminder to Slack thread
  2. Tag the on-call group / PagerDuty integration
  3. If 2x SLA breached, auto-reject and escalate to manual

The timer runs as an asyncio background task started during app lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine

from ..config import settings
from .session import DiagnosisSession, SessionPhase, session_store

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SLA configuration
# ---------------------------------------------------------------------------
DEFAULT_SLA_SECONDS = {
    "critical": 5 * 60,     # 5 minutes
    "warning": 15 * 60,     # 15 minutes
    "info": 60 * 60,        # 1 hour
}


@dataclass
class EscalationConfig:
    """Configuration for the escalation timer."""

    sla_seconds: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SLA_SECONDS))
    check_interval: int = 30  # seconds between checks
    escalation_group: str = ""  # Slack user group to tag on escalation
    auto_reject_multiplier: float = 2.0  # auto-reject at 2x SLA
    enabled: bool = True


# ---------------------------------------------------------------------------
# Escalation state per session
# ---------------------------------------------------------------------------
@dataclass
class EscalationState:
    """Tracks escalation state for a single session."""

    session_id: str
    sla_deadline: datetime
    auto_reject_deadline: datetime
    reminder_sent: bool = False
    auto_rejected: bool = False


# ---------------------------------------------------------------------------
# Escalation timer
# ---------------------------------------------------------------------------
class EscalationTimer:
    """Background task that monitors sessions awaiting approval.

    Usage:
        timer = EscalationTimer(config, on_escalate=my_callback)
        await timer.start()   # runs until cancelled
        timer.stop()
    """

    def __init__(
        self,
        config: EscalationConfig | None = None,
        on_escalate: Callable[[DiagnosisSession, str], Coroutine] | None = None,
    ) -> None:
        self.config = config or EscalationConfig()
        self.on_escalate = on_escalate
        self._tracked: dict[str, EscalationState] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background check loop."""
        if not self.config.enabled:
            logger.info("Escalation timer disabled")
            return

        self._task = asyncio.current_task() or asyncio.ensure_future(self._loop())
        logger.info(
            "Escalation timer started (check every %ds, SLAs: %s)",
            self.config.check_interval,
            self.config.sla_seconds,
        )

    async def run(self) -> None:
        """Run the escalation timer loop (call this from a background task)."""
        if not self.config.enabled:
            return
        await self._loop()

    def stop(self) -> None:
        """Stop the background check loop."""
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Escalation timer stopped")

    async def _loop(self) -> None:
        """Main check loop — runs forever until cancelled."""
        try:
            while True:
                await self._check_sessions()
                await asyncio.sleep(self.config.check_interval)
        except asyncio.CancelledError:
            logger.info("Escalation timer loop cancelled")

    async def _check_sessions(self) -> None:
        """Check all sessions awaiting approval for SLA breaches."""
        now = datetime.now(timezone.utc)

        # Find sessions awaiting approval
        for session in session_store.active_sessions():
            if session.phase != SessionPhase.AWAITING_APPROVAL:
                # Clean up tracking for sessions that moved on
                self._tracked.pop(session.id, None)
                continue

            # Start tracking if not already
            if session.id not in self._tracked:
                sla_secs = self._get_sla_seconds(session)
                self._tracked[session.id] = EscalationState(
                    session_id=session.id,
                    sla_deadline=session.updated_at + timedelta(seconds=sla_secs),
                    auto_reject_deadline=session.updated_at + timedelta(
                        seconds=int(sla_secs * self.config.auto_reject_multiplier)
                    ),
                )

            state = self._tracked[session.id]

            # Check auto-reject deadline (2x SLA)
            if not state.auto_rejected and now >= state.auto_reject_deadline:
                state.auto_rejected = True
                await self._handle_auto_reject(session, state)
                self._tracked.pop(session.id, None)
                continue

            # Check SLA deadline (1x)
            if not state.reminder_sent and now >= state.sla_deadline:
                state.reminder_sent = True
                await self._handle_sla_breach(session, state)

        # Clean up tracked sessions that are no longer active
        active_ids = {s.id for s in session_store.active_sessions()}
        stale = [sid for sid in self._tracked if sid not in active_ids]
        for sid in stale:
            del self._tracked[sid]

    def _get_sla_seconds(self, session: DiagnosisSession) -> int:
        """Get the SLA timeout based on alert severity."""
        severity = session.alert.severity.lower()
        return self.config.sla_seconds.get(severity, self.config.sla_seconds.get("warning", 900))

    async def _handle_sla_breach(self, session: DiagnosisSession, state: EscalationState) -> None:
        """First escalation — send reminder with on-call tag."""
        sla_secs = self._get_sla_seconds(session)
        logger.warning(
            "SLA BREACH: session %s has been awaiting approval for >%ds (severity=%s)",
            session.id, sla_secs, session.alert.severity,
        )

        # Build escalation message
        group_mention = ""
        if self.config.escalation_group:
            group_mention = f" <!subteam^{self.config.escalation_group}>"

        message = (
            f"⏰ *SLA BREACH* — Fix awaiting approval for >{sla_secs // 60} minutes{group_mention}\n\n"
            f"*Alert:* {session.alert.alert_name}\n"
            f"*Namespace:* `{session.alert.namespace}`\n"
        )
        if session.fix_proposal:
            message += f"*Fix:* {session.fix_proposal.summary}\n"
            message += f"*Risk:* {session.fix_proposal.risk_level.value}\n"

        auto_reject_mins = int(
            (state.auto_reject_deadline - datetime.now(timezone.utc)).total_seconds() / 60
        )
        message += f"\n⚠️ Fix will be *auto-rejected* in ~{max(1, auto_reject_mins)} minutes if no action is taken."

        if self.on_escalate:
            await self.on_escalate(session, message)

    async def _handle_auto_reject(self, session: DiagnosisSession, state: EscalationState) -> None:
        """Second escalation — auto-reject the fix and mark for manual handling."""
        logger.error(
            "AUTO-REJECT: session %s exceeded 2x SLA, auto-rejecting fix",
            session.id,
        )

        session.reject("system:escalation_timer")

        message = (
            f"🚨 *AUTO-REJECTED* — Fix exceeded 2x SLA with no human response\n\n"
            f"*Alert:* {session.alert.alert_name}\n"
            f"*Namespace:* `{session.alert.namespace}`\n"
            f"*Action required:* Manual investigation needed. The automated fix was rejected due to no response.\n"
        )

        if self.on_escalate:
            await self.on_escalate(session, message)

    @property
    def tracked_count(self) -> int:
        """Number of sessions currently being tracked for SLA."""
        return len(self._tracked)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
escalation_timer = EscalationTimer()
