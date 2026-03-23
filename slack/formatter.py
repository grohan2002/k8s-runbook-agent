"""Slack Block Kit message formatters.

Converts DiagnosisSession state into rich Slack messages with:
- Alert header with severity colors
- Diagnosis details with evidence
- Fix proposal with dry-run output
- Approve / Reject interactive buttons
- Escalation notices

Uses Slack Block Kit: https://api.slack.com/block-kit
"""

from __future__ import annotations

from typing import Any

from ..agent.session import DiagnosisSession, SessionPhase
from ..models import Confidence, RiskLevel

# ---------------------------------------------------------------------------
# Color coding
# ---------------------------------------------------------------------------
SEVERITY_COLORS = {
    "critical": "#FF0000",
    "warning": "#FFA500",
    "info": "#2196F3",
}

CONFIDENCE_EMOJI = {
    Confidence.HIGH: ":large_green_circle:",
    Confidence.MEDIUM: ":large_yellow_circle:",
    Confidence.LOW: ":red_circle:",
}

RISK_EMOJI = {
    RiskLevel.LOW: ":white_check_mark:",
    RiskLevel.MEDIUM: ":warning:",
    RiskLevel.HIGH: ":bangbang:",
    RiskLevel.CRITICAL: ":rotating_light:",
}


# ---------------------------------------------------------------------------
# Main formatters
# ---------------------------------------------------------------------------
def format_alert_received(session: DiagnosisSession) -> dict[str, Any]:
    """Initial notification when an alert starts investigation."""
    alert = session.alert
    color = SEVERITY_COLORS.get(alert.severity, "#808080")

    return {
        "attachments": [
            {
                "color": color,
                "blocks": [
                    _header(f":rotating_light: Alert: {alert.alert_name}"),
                    _section(
                        f"*Severity:* `{alert.severity}`\n"
                        f"*Namespace:* `{alert.namespace}`\n"
                        + (f"*Pod:* `{alert.pod}`\n" if alert.pod else "")
                        + f"*Summary:* {alert.summary}"
                    ),
                    _context(f"Session: {session.id} | Status: :mag: Investigating..."),
                ],
            }
        ],
    }


def format_diagnosis_result(session: DiagnosisSession) -> dict[str, Any]:
    """Full diagnosis result with fix proposal and approval buttons."""
    alert = session.alert
    color = SEVERITY_COLORS.get(alert.severity, "#808080")
    blocks: list[dict[str, Any]] = []

    # Header
    blocks.append(_header(f":stethoscope: Diagnosis: {alert.alert_name}"))

    # Alert info
    blocks.append(
        _section(
            f"*Namespace:* `{alert.namespace}`"
            + (f" | *Pod:* `{alert.pod}`" if alert.pod else "")
            + f" | *Severity:* `{alert.severity}`"
        )
    )
    blocks.append(_divider())

    # Diagnosis
    if session.diagnosis:
        diag = session.diagnosis
        confidence_icon = CONFIDENCE_EMOJI.get(diag.confidence, ":question:")

        blocks.append(
            _section(
                f"*Root Cause:* {diag.root_cause}\n"
                f"*Confidence:* {confidence_icon} {diag.confidence.value.upper()}"
            )
        )

        if diag.evidence:
            evidence_text = "\n".join(f"• {e}" for e in diag.evidence[:8])
            blocks.append(_section(f"*Evidence:*\n{evidence_text}"))

        if diag.ruled_out:
            ruled_out_text = "\n".join(f"• {r}" for r in diag.ruled_out[:5])
            blocks.append(_section(f"*Ruled Out:*\n{ruled_out_text}"))

        blocks.append(_divider())

    # Fix proposal
    if session.fix_proposal:
        fix = session.fix_proposal
        risk_icon = RISK_EMOJI.get(fix.risk_level, ":question:")

        blocks.append(
            _section(
                f"*:wrench: Proposed Fix:* {fix.summary}\n"
                f"*Risk:* {risk_icon} {fix.risk_level.value.upper()}"
            )
        )

        if fix.description:
            # Truncate long descriptions
            desc = fix.description[:800] + ("..." if len(fix.description) > 800 else "")
            blocks.append(_section(f"*Description:*\n{desc}"))

        if fix.dry_run_output:
            dry_run = fix.dry_run_output[:600] + ("..." if len(fix.dry_run_output) > 600 else "")
            blocks.append(_section(f"*Dry Run:*\n```{dry_run}```"))

        if fix.rollback_plan:
            blocks.append(_section(f"*Rollback Plan:*\n{fix.rollback_plan}"))

        if fix.requires_human_values:
            fields = ", ".join(f"`{f}`" for f in fix.human_value_fields)
            blocks.append(
                _section(
                    f":pencil2: *Human input needed for:* {fields}\n"
                    "_Please provide these values before approving._"
                )
            )

        blocks.append(_divider())

        # Approval buttons
        blocks.append(
            _actions(
                session.id,
                [
                    _button("Approve Fix", f"approve:{session.id}", "primary"),
                    _button("Reject", f"reject:{session.id}", "danger"),
                    _button("Show Details", f"details:{session.id}"),
                ],
            )
        )

    # Footer context
    blocks.append(
        _context(
            f"Session: {session.id} | "
            f"Tool calls: {session.tool_calls_made} | "
            f"Tokens: {session.total_tokens_used:,}"
        )
    )

    return {"attachments": [{"color": color, "blocks": blocks}]}


def format_escalation(session: DiagnosisSession) -> dict[str, Any]:
    """Escalation message when the agent cannot determine a fix."""
    alert = session.alert

    blocks = [
        _header(f":sos: Escalation: {alert.alert_name}"),
        _section(
            f"*Namespace:* `{alert.namespace}`"
            + (f" | *Pod:* `{alert.pod}`" if alert.pod else "")
        ),
        _divider(),
        _section(f"*Reason:* {session.error or 'Agent could not determine root cause.'}"),
    ]

    # Include diagnosis if partial
    if session.diagnosis:
        blocks.append(
            _section(
                f"*Partial Diagnosis:* {session.diagnosis.root_cause}\n"
                f"*Confidence:* {session.diagnosis.confidence.value} (too low to propose fix)"
            )
        )

    blocks.append(
        _section(
            ":point_right: *Action needed:* On-call engineer should investigate manually.\n"
            f"Use `/k8s-diag details {session.id}` to view the full investigation log."
        )
    )
    blocks.append(
        _context(
            f"Session: {session.id} | "
            f"Tool calls: {session.tool_calls_made} | "
            f"Tokens: {session.total_tokens_used:,}"
        )
    )

    return {"attachments": [{"color": "#FF0000", "blocks": blocks}]}


def format_approval_confirmation(session: DiagnosisSession, approver: str) -> dict[str, Any]:
    """Confirmation that a fix was approved."""
    return {
        "blocks": [
            _section(
                f":white_check_mark: *Fix approved by <@{approver}>*\n"
                f"*Fix:* {session.fix_proposal.summary if session.fix_proposal else 'N/A'}\n"
                f"*Session:* {session.id}"
            ),
            _context("Execution will begin shortly. Watch this thread for results."),
        ]
    }


def format_rejection(session: DiagnosisSession, approver: str) -> dict[str, Any]:
    """Confirmation that a fix was rejected."""
    return {
        "blocks": [
            _section(
                f":x: *Fix rejected by <@{approver}>*\n"
                f"*Session:* {session.id}\n"
                "The incident remains open for manual resolution."
            ),
        ]
    }


def format_execution_result(session: DiagnosisSession) -> dict[str, Any]:
    """Result after a fix has been executed."""
    result = session.approval.execution_result or "No output"
    success = session.phase == SessionPhase.RESOLVED

    emoji = ":white_check_mark:" if success else ":x:"
    return {
        "blocks": [
            _section(f"{emoji} *Execution Result:*\n```{result[:1500]}```"),
            _context(f"Session: {session.id} | Phase: {session.phase.value}"),
        ]
    }


def format_session_details(session: DiagnosisSession) -> dict[str, Any]:
    """Detailed session dump for the /k8s-diag details command."""
    blocks = [
        _header(f":clipboard: Session Details: {session.id}"),
        _section(
            f"*Alert:* {session.alert.alert_name}\n"
            f"*Namespace:* `{session.alert.namespace}`\n"
            + (f"*Pod:* `{session.alert.pod}`\n" if session.alert.pod else "")
            + f"*Phase:* {session.phase.value}\n"
            f"*Created:* {session.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"*Updated:* {session.updated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"*Tool calls:* {session.tool_calls_made}\n"
            f"*Tokens:* {session.total_tokens_used:,}"
        ),
    ]

    if session.runbook:
        blocks.append(_section(f"*Matched Runbook:* {session.runbook.metadata.title}"))

    if session.diagnosis:
        d = session.diagnosis
        blocks.append(_divider())
        blocks.append(
            _section(
                f"*Diagnosis:*\n"
                f"Root Cause: {d.root_cause}\n"
                f"Confidence: {d.confidence.value}\n"
                f"Evidence:\n" + "\n".join(f"  • {e}" for e in d.evidence)
            )
        )

    if session.fix_proposal:
        f = session.fix_proposal
        blocks.append(_divider())
        blocks.append(
            _section(
                f"*Fix Proposal:*\n"
                f"Summary: {f.summary}\n"
                f"Risk: {f.risk_level.value}\n"
                f"Description: {f.description[:500]}"
            )
        )

    if session.error:
        blocks.append(_divider())
        blocks.append(_section(f"*Error:* {session.error}"))

    return {"blocks": blocks}


# ---------------------------------------------------------------------------
# Block Kit helpers
# ---------------------------------------------------------------------------
def _header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150], "emoji": True}}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _divider() -> dict[str, Any]:
    return {"type": "divider"}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _actions(block_id: str, buttons: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "actions", "block_id": block_id, "elements": buttons}


def _button(text: str, action_id: str, style: str | None = None) -> dict[str, Any]:
    btn: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "action_id": action_id,
    }
    if style:
        btn["style"] = style
    return btn
