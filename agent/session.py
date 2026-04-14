"""Session state manager — one session per alert investigation.

Tracks the lifecycle:  ALERT → INVESTIGATING → DIAGNOSED → FIX_PROPOSED → AWAITING_APPROVAL → EXECUTING → RESOLVED / ESCALATED

All state transitions are persisted to PostgreSQL (when configured) and logged
to the audit_log table for compliance and post-incident review.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..models import (
    ApprovalState,
    ApprovalStatus,
    Confidence,
    Diagnosis,
    DiagnosticRunbook,
    FixProposal,
    GrafanaAlert,
    RiskLevel,
)

logger = logging.getLogger(__name__)


def _fire_and_forget(coro):
    """Schedule a coroutine without awaiting it. Logs exceptions."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # No event loop — skip persistence (e.g. unit tests)

    task = loop.create_task(coro)
    task.add_done_callback(
        lambda t: logger.error("Background persist failed: %s", t.exception())
        if t.exception() else None
    )


class SessionPhase(str, Enum):
    """Lifecycle phases of a diagnosis session."""

    ALERT_RECEIVED = "alert_received"
    INVESTIGATING = "investigating"
    DIAGNOSED = "diagnosed"
    FIX_PROPOSED = "fix_proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"


class DiagnosisSession:
    """State container for a single alert investigation.

    The orchestrator drives the session through phases; Slack handlers
    update approval state.  All conversation messages are stored so the
    Anthropic API can receive the full context on each turn.
    """

    def __init__(self, alert: GrafanaAlert) -> None:
        self.id: str = f"diag-{uuid.uuid4().hex[:12]}"
        self.alert = alert
        self.phase = SessionPhase.ALERT_RECEIVED
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at

        # Matched runbook (if any)
        self.runbook: DiagnosticRunbook | None = None

        # Conversation history for Anthropic API (list of message dicts)
        self.messages: list[dict[str, Any]] = []

        # Diagnosis result
        self.diagnosis: Diagnosis | None = None

        # Fix proposal + approval
        self.fix_proposal: FixProposal | None = None
        self.approval: ApprovalState = ApprovalState(incident_id=self.id)

        # Multi-agent tracking
        self.agent_type: str = "single"        # "single" | "multi_agent"
        self.specialist_domain: str = ""       # "pod" | "network" | "infrastructure" | "application"
        self.triage_result: dict | None = None # Stored triage output

        # External incident tracking (PagerDuty, OpsGenie)
        self.incident_ids: dict[str, str] = {}

        # Bookkeeping
        self.tool_calls_made: int = 0
        self.tools_called: set[str] = set()  # specific tool names called
        self.total_tokens_used: int = 0
        self.fix_confidence: Any | None = None  # FixConfidence from incident_memory
        self.error: str | None = None

        # Slack thread reference for sending updates
        self.slack_thread_ts: str | None = None
        self.slack_channel: str | None = None

    # ------------------------------------------------------------------
    # Phase transitions
    # ------------------------------------------------------------------
    def transition(self, new_phase: SessionPhase, actor: str = "system") -> None:
        """Move to a new phase with logging, persistence, and audit trail."""
        old = self.phase
        self.phase = new_phase
        self.updated_at = datetime.now(timezone.utc)
        logger.info(
            "Session %s: %s → %s (alert=%s, ns=%s)",
            self.id,
            old.value,
            new_phase.value,
            self.alert.alert_name,
            self.alert.namespace,
        )

        # Persist to PostgreSQL (non-blocking)
        _fire_and_forget(self._persist_and_audit(old.value, new_phase.value, actor))

        # Record to incident memory on terminal phases
        if new_phase in (SessionPhase.RESOLVED, SessionPhase.ESCALATED, SessionPhase.FAILED):
            _fire_and_forget(self._record_to_memory())

    async def _record_to_memory(self) -> None:
        """Persist this session's learnings to incident memory."""
        try:
            from .incident_memory import incident_memory

            await incident_memory.record(self)
        except Exception:
            logger.warning("Failed to record incident memory for %s", self.id, exc_info=True)

    async def _persist_and_audit(self, old_phase: str, new_phase: str, actor: str) -> None:
        """Save session state and write audit log entry."""
        from ..db import save_session, write_audit_log

        await save_session(self)
        await write_audit_log(
            session_id=self.id,
            event_type="phase_transition",
            actor=actor,
            old_phase=old_phase,
            new_phase=new_phase,
        )

    # ------------------------------------------------------------------
    # Conversation management
    # ------------------------------------------------------------------
    def add_user_message(self, content: str) -> None:
        """Add a user-role message (alert context, approval response, etc.)."""
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str | list[dict]) -> None:
        """Add an assistant-role message (Claude's response)."""
        self.messages.append({"role": "assistant", "content": content})

    def add_tool_result(self, tool_use_id: str, content: str, is_error: bool = False) -> None:
        """Add a tool result message."""
        self.messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        })
        self.tool_calls_made += 1

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------
    def set_diagnosis(
        self,
        root_cause: str,
        confidence: Confidence,
        evidence: list[str],
        ruled_out: list[str] | None = None,
    ) -> None:
        self.diagnosis = Diagnosis(
            root_cause=root_cause,
            confidence=confidence,
            evidence=evidence,
            ruled_out=ruled_out or [],
        )
        self.transition(SessionPhase.DIAGNOSED)

    # ------------------------------------------------------------------
    # Fix proposal
    # ------------------------------------------------------------------
    def set_fix_proposal(
        self,
        summary: str,
        description: str,
        risk_level: RiskLevel,
        dry_run_output: str = "",
        rollback_plan: str = "",
        requires_human_values: bool = False,
        human_value_fields: list[str] | None = None,
    ) -> None:
        self.fix_proposal = FixProposal(
            summary=summary,
            description=description,
            risk_level=risk_level,
            dry_run_output=dry_run_output,
            rollback_plan=rollback_plan,
            requires_human_values=requires_human_values,
            human_value_fields=human_value_fields or [],
        )
        self.approval.fix_proposal = self.fix_proposal
        self.transition(SessionPhase.FIX_PROPOSED)

    # ------------------------------------------------------------------
    # Approval
    # ------------------------------------------------------------------
    def request_approval(self) -> None:
        """Move to awaiting human approval."""
        self.transition(SessionPhase.AWAITING_APPROVAL)

    def approve(self, approver: str) -> None:
        self.approval.status = ApprovalStatus.APPROVED
        self.approval.approved_by = approver
        self.approval.approved_at = datetime.now(timezone.utc)
        self.transition(SessionPhase.EXECUTING, actor=approver)

    def reject(self, approver: str) -> None:
        self.approval.status = ApprovalStatus.REJECTED
        self.approval.approved_by = approver
        self.approval.approved_at = datetime.now(timezone.utc)
        self.transition(SessionPhase.RESOLVED, actor=approver)

    def mark_resolved(self, result: str) -> None:
        self.approval.executed = True
        self.approval.execution_result = result
        self.transition(SessionPhase.RESOLVED)

    def escalate(self, reason: str) -> None:
        self.error = reason
        self.transition(SessionPhase.ESCALATED)

    def fail(self, error: str) -> None:
        self.error = error
        self.transition(SessionPhase.FAILED)

    # ------------------------------------------------------------------
    # Serialization for Slack / logging
    # ------------------------------------------------------------------
    def summary_text(self) -> str:
        """Human-readable summary of current session state."""
        lines = [
            f"🔍 **Incident:** {self.id}",
            f"📢 **Alert:** {self.alert.alert_name}",
            f"📦 **Namespace:** {self.alert.namespace}",
        ]
        if self.alert.pod:
            lines.append(f"🔹 **Pod:** {self.alert.pod}")
        lines.append(f"⏱️ **Phase:** {self.phase.value}")

        if self.diagnosis:
            lines.append(f"\n🩺 **Root Cause:** {self.diagnosis.root_cause}")
            lines.append(f"📊 **Confidence:** {self.diagnosis.confidence.value}")
            if self.diagnosis.evidence:
                lines.append("📋 **Evidence:**")
                for e in self.diagnosis.evidence:
                    lines.append(f"  • {e}")

        if self.fix_proposal:
            lines.append(f"\n🔧 **Proposed Fix:** {self.fix_proposal.summary}")
            lines.append(f"⚠️ **Risk:** {self.fix_proposal.risk_level.value}")
            if self.fix_proposal.rollback_plan:
                lines.append(f"↩️ **Rollback:** {self.fix_proposal.rollback_plan}")
            if self.fix_proposal.requires_human_values:
                lines.append(f"✏️ **Needs human input for:** {', '.join(self.fix_proposal.human_value_fields)}")

        lines.append(f"\n🔢 Tool calls: {self.tool_calls_made} | Tokens: {self.total_tokens_used}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session store — in-memory with PostgreSQL write-through
# ---------------------------------------------------------------------------
class SessionStore:
    """In-memory session store with PostgreSQL persistence.

    The in-memory dict is the primary lookup for performance.
    PostgreSQL is the durable backing store — written to on every transition.
    On startup, active sessions can be rehydrated from PG if needed.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, DiagnosisSession] = {}

    def create(self, alert: GrafanaAlert) -> DiagnosisSession:
        session = DiagnosisSession(alert)
        self._sessions[session.id] = session
        logger.info("Created session %s for alert %s", session.id, alert.alert_name)
        # Initial persist
        _fire_and_forget(self._persist_new(session))
        return session

    async def _persist_new(self, session: DiagnosisSession) -> None:
        from ..db import save_session, write_audit_log

        await save_session(session)
        await write_audit_log(
            session_id=session.id,
            event_type="session_created",
            details={
                "alert_name": session.alert.alert_name,
                "namespace": session.alert.namespace,
                "severity": session.alert.severity,
            },
        )

    def get(self, session_id: str) -> DiagnosisSession | None:
        return self._sessions.get(session_id)

    def get_by_fingerprint(self, fingerprint: str) -> DiagnosisSession | None:
        """Find an active session for a deduplicated alert."""
        for s in self._sessions.values():
            if (
                s.alert.fingerprint == fingerprint
                and s.phase
                not in (SessionPhase.RESOLVED, SessionPhase.FAILED, SessionPhase.ESCALATED)
            ):
                return s
        return None

    def active_sessions(self) -> list[DiagnosisSession]:
        return [
            s
            for s in self._sessions.values()
            if s.phase not in (SessionPhase.RESOLVED, SessionPhase.FAILED, SessionPhase.ESCALATED)
        ]

    def all_sessions(self) -> list[DiagnosisSession]:
        return list(self._sessions.values())


# Module-level singleton
session_store = SessionStore()
