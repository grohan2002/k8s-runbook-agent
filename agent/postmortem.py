"""Post-Mortem Generator — auto-generates a markdown post-mortem on session resolution.

Triggered from session.transition() when the session reaches a terminal phase
(RESOLVED, ESCALATED, or FAILED). Produces a human-readable post-mortem that can
be copy-pasted into Confluence/Notion and is also posted in the Slack thread.

Structure:
  1. Summary — one-line incident overview
  2. Timeline — from audit_log
  3. Root Cause — from session.diagnosis
  4. Fix Applied — from fix_proposal + execution_result
  5. Lessons Learned — from incident memory (similar past incidents)
  6. Metadata — session ID, duration, tokens, tool calls, incident references
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .session import DiagnosisSession, SessionPhase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------
async def generate_postmortem(session: DiagnosisSession) -> str:
    """Build a full markdown post-mortem for a resolved/escalated/failed session."""
    sections: list[str] = []

    # Header
    sections.append(f"# Post-Mortem: {session.alert.alert_name}")
    sections.append(_build_summary(session))

    # Timeline (from audit log)
    timeline = await _build_timeline(session)
    if timeline:
        sections.append("## Timeline\n" + timeline)

    # Root cause
    root_cause = _build_root_cause(session)
    if root_cause:
        sections.append("## Root Cause\n" + root_cause)

    # Fix applied
    fix = _build_fix_applied(session)
    if fix:
        sections.append("## Fix Applied\n" + fix)

    # Lessons learned
    lessons = await _build_lessons_learned(session)
    if lessons:
        sections.append("## Lessons Learned\n" + lessons)

    # Metadata
    sections.append("## Metadata\n" + _build_metadata(session))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def _build_summary(session: DiagnosisSession) -> str:
    """One-paragraph summary of what happened."""
    outcome = {
        SessionPhase.RESOLVED: "✅ Resolved",
        SessionPhase.ESCALATED: "⚠️ Escalated to humans",
        SessionPhase.FAILED: "❌ Failed",
    }.get(session.phase, session.phase.value)

    duration = session.updated_at - session.created_at
    duration_min = round(duration.total_seconds() / 60, 1)

    lines = [
        f"**Outcome:** {outcome}",
        f"**Alert:** `{session.alert.alert_name}`",
        f"**Namespace:** `{session.alert.namespace}`",
    ]
    if session.alert.pod:
        lines.append(f"**Pod:** `{session.alert.pod}`")
    lines.append(f"**Severity:** `{session.alert.severity}`")
    lines.append(f"**Duration:** {duration_min} minutes")

    if session.alert.slo_name:
        budget = (
            f" (error budget: {session.alert.error_budget_remaining}%)"
            if session.alert.error_budget_remaining is not None
            else ""
        )
        lines.append(f"**SLO Affected:** `{session.alert.slo_name}`{budget}")

    return "\n".join(lines)


async def _build_timeline(session: DiagnosisSession) -> str:
    """Timeline of phase transitions from the audit log."""
    try:
        from ..db import get_audit_log
        entries = await get_audit_log(session.id, limit=100)
    except Exception:
        logger.warning("Failed to load audit log for postmortem", exc_info=True)
        entries = []

    if not entries:
        # Fallback: just show the two timestamps we have
        return (
            f"- {_fmt_ts(session.created_at)}: Session created\n"
            f"- {_fmt_ts(session.updated_at)}: Final phase `{session.phase.value}`"
        )

    # Reverse to chronological order (DB returns DESC)
    entries = list(reversed(entries))
    lines = []
    for e in entries:
        ts = e.get("created_at", "")
        event = e.get("event_type", "")
        actor = e.get("actor", "system")
        old = e.get("old_phase")
        new = e.get("new_phase")

        if event == "phase_transition" and old and new:
            lines.append(f"- {ts}: `{old}` → `{new}` (by {actor})")
        else:
            lines.append(f"- {ts}: {event} (by {actor})")

    return "\n".join(lines)


def _build_root_cause(session: DiagnosisSession) -> str:
    """Root cause with evidence — or note that none was determined."""
    if not session.diagnosis:
        if session.phase == SessionPhase.FAILED and session.error:
            return f"_No diagnosis was produced. Agent error:_ `{session.error}`"
        if session.phase == SessionPhase.ESCALATED:
            return f"_No diagnosis. Escalation reason:_ {session.error or 'unknown'}"
        return "_No diagnosis was produced._"

    d = session.diagnosis
    lines = [
        f"**Determined Cause:** {d.root_cause}",
        f"**Confidence:** {d.confidence.value.upper()}",
    ]

    if d.evidence:
        lines.append("\n**Evidence collected:**")
        for e in d.evidence:
            lines.append(f"- {e}")

    if d.ruled_out:
        lines.append("\n**Alternatives ruled out:**")
        for r in d.ruled_out:
            lines.append(f"- {r}")

    return "\n".join(lines)


def _build_fix_applied(session: DiagnosisSession) -> str:
    """Fix proposal + execution result."""
    if not session.fix_proposal:
        return "_No fix was proposed._"

    f = session.fix_proposal
    lines = [
        f"**Proposed Fix:** {f.summary}",
        f"**Risk Level:** {f.risk_level.value.upper()}",
    ]

    if f.description:
        lines.append(f"\n**Description:**\n{f.description}")

    if f.rollback_plan:
        lines.append(f"\n**Rollback Plan:**\n{f.rollback_plan}")

    # Approval / execution outcome
    appr = session.approval
    if appr.status.value == "approved":
        approver = appr.approved_by or "unknown"
        lines.append(f"\n**Approved by:** `{approver}`")
        if appr.executed:
            lines.append(f"**Executed:** yes")
            if appr.execution_result:
                lines.append(f"**Execution result:** {appr.execution_result}")
        else:
            lines.append(f"**Executed:** no")
    elif appr.status.value == "rejected":
        lines.append(f"\n**Rejected by:** `{appr.approved_by or 'unknown'}`")
    else:
        lines.append(f"\n**Status:** {appr.status.value}")

    # Fix confidence (if computed)
    fc = session.fix_confidence
    if fc is not None:
        lines.append(
            f"\n**Fix Confidence at Approval Time:** {fc.percentage}% "
            f"({fc.fix_success_count}/{fc.fix_total_count} past successes)"
            if fc.has_history
            else f"\n**Fix Confidence at Approval Time:** {fc.percentage}% (no prior history)"
        )

    return "\n".join(lines)


async def _build_lessons_learned(session: DiagnosisSession) -> str:
    """Lessons from incident memory — similar past incidents + recurring patterns."""
    try:
        from .incident_memory import incident_memory
        memory = await incident_memory.recall(session.alert)
    except Exception:
        logger.debug("Incident memory recall failed for postmortem", exc_info=True)
        return ""

    if not memory.has_data:
        return "_This is the first incident of this type in memory — no prior lessons._"

    lines: list[str] = []

    if memory.similar_incidents:
        lines.append("**Similar past incidents:**")
        for i, inc in enumerate(memory.similar_incidents[:5], 1):
            resolved = inc.resolved_at.strftime("%Y-%m-%d") if inc.resolved_at else "unknown date"
            lines.append(
                f"{i}. [{resolved}] `{inc.root_cause}` → "
                f"_{inc.fix_summary}_ ({inc.outcome})"
            )

    if memory.fix_success_rates:
        lines.append("\n**Fix success rates for this alert type:**")
        for rate in memory.fix_success_rates[:5]:
            lines.append(f"- `{rate.fix_summary}`: {rate.rate_str}")

    if memory.recurring_patterns:
        lines.append("\n**⚠️ Recurring pattern detected:**")
        for p in memory.recurring_patterns:
            lines.append(
                f"- `{p.alert_name}` with root cause `{p.root_cause}` "
                f"has occurred {p.occurrences}x. "
                f"Consider a permanent fix rather than repeated remediation."
            )

    return "\n".join(lines) if lines else ""


def _build_metadata(session: DiagnosisSession) -> str:
    """Session ID, tool calls, tokens, external incident refs."""
    lines = [
        f"- **Session ID:** `{session.id}`",
        f"- **Tool calls:** {session.tool_calls_made}",
        f"- **Tools used:** {', '.join(sorted(session.tools_called)) or 'none'}",
        f"- **Tokens used:** {session.total_tokens_used:,}",
    ]

    if session.runbook:
        lines.append(f"- **Runbook matched:** `{session.runbook.metadata.id}`")

    if session.agent_type != "single":
        lines.append(f"- **Agent type:** {session.agent_type}")
        if session.specialist_domain:
            lines.append(f"- **Specialist domain:** `{session.specialist_domain}`")

    # External incident refs
    if session.incident_ids:
        lines.append("- **External incidents:**")
        for provider, inc_id in session.incident_ids.items():
            lines.append(f"  - {provider}: `{inc_id}`")

    # Slack thread URL if available
    if session.slack_channel and session.slack_thread_ts:
        ts_str = session.slack_thread_ts.replace(".", "")
        slack_url = f"https://slack.com/archives/{session.slack_channel}/p{ts_str}"
        lines.append(f"- **Slack thread:** {slack_url}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fmt_ts(ts: Any) -> str:
    """Format a timestamp for the timeline."""
    if isinstance(ts, datetime):
        return ts.isoformat(timespec="seconds")
    return str(ts)
