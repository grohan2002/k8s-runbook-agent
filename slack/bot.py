"""Slack bot — sends messages, handles interactive button callbacks.

Uses slack_sdk for Slack API calls and provides:
  1. post_diagnosis_result()  — called by orchestrator when investigation completes
  2. Interactive endpoint    — handles Approve/Reject button clicks
  3. Slash command endpoint  — /k8s-diag status, /k8s-diag details <id>

Production hardening:
  - Interactive callbacks return 200 immediately, execute in background tasks
  - Idempotency keys prevent duplicate actions from Slack retries
  - All Slack API calls use asyncio.to_thread() to avoid blocking the event loop

Slack setup requirements:
  - Bot token scopes: chat:write, commands, incoming-webhook
  - Interactive Components → Request URL: https://<host>/slack/interactions
  - Slash Command /k8s-diag → Request URL: https://<host>/slack/commands
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Header, HTTPException, Request, Response

from ..agent.session import DiagnosisSession, SessionPhase, session_store
from ..config import settings
from ..observability.rate_limit import slack_limiter
from .formatter import (
    format_alert_received,
    format_approval_confirmation,
    format_diagnosis_result,
    format_escalation,
    format_execution_result,
    format_rejection,
    format_session_details,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack"])


# ---------------------------------------------------------------------------
# Idempotency — prevent duplicate action processing from Slack retries
# ---------------------------------------------------------------------------
class _IdempotencyCache:
    """Simple bounded LRU cache of processed action IDs.

    Slack may retry interactive payloads if it doesn't get 200 within 3s.
    We track processed actions to avoid double-approvals/rejections.
    """

    def __init__(self, max_size: int = 1000) -> None:
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._max_size = max_size

    def check_and_mark(self, key: str) -> bool:
        """Return True if this key is NEW (not seen before). Marks it as processed."""
        if key in self._seen:
            return False  # Already processed
        if len(self._seen) >= self._max_size:
            self._seen.popitem(last=False)  # Evict oldest
        self._seen[key] = time.time()
        return True


_idempotency = _IdempotencyCache()


# ---------------------------------------------------------------------------
# Slack Web API client (lazy init)
# ---------------------------------------------------------------------------
_slack_client = None


def _get_slack_client():
    """Lazily initialize the Slack WebClient."""
    global _slack_client
    if _slack_client is None:
        from slack_sdk import WebClient

        _slack_client = WebClient(token=settings.slack_bot_token)
    return _slack_client


# ---------------------------------------------------------------------------
# Outbound message posting
# ---------------------------------------------------------------------------
async def _slack_post(**kwargs) -> dict:
    """Run a synchronous Slack API call in a thread to avoid blocking the event loop."""
    client = _get_slack_client()
    return await asyncio.to_thread(client.chat_postMessage, **kwargs)


async def post_alert_received(session: DiagnosisSession) -> None:
    """Post initial 'investigating' message to Slack when an alert arrives."""
    channel = settings.slack_channel_id

    message = format_alert_received(session)
    try:
        result = await _slack_post(
            channel=channel,
            text=f"🔍 Investigating alert: {session.alert.alert_name}",
            **message,
        )
        # Store thread reference for follow-up messages
        session.slack_thread_ts = result["ts"]
        session.slack_channel = channel
        logger.info("Posted alert notification for session %s (ts=%s)", session.id, result["ts"])
    except Exception:
        logger.exception("Failed to post alert notification for session %s", session.id)


async def post_diagnosis_result(session: DiagnosisSession) -> None:
    """Post the diagnosis result (or escalation) to Slack."""
    channel = session.slack_channel or settings.slack_channel_id

    if session.phase == SessionPhase.ESCALATED:
        message = format_escalation(session)
        fallback_text = f"⚠️ Escalation: {session.alert.alert_name}"
    elif session.phase in (SessionPhase.AWAITING_APPROVAL, SessionPhase.FIX_PROPOSED):
        message = format_diagnosis_result(session)
        fallback_text = f"🩺 Diagnosis ready: {session.alert.alert_name}"
    elif session.phase == SessionPhase.FAILED:
        message = format_escalation(session)
        fallback_text = f"❌ Investigation failed: {session.alert.alert_name}"
    else:
        message = format_diagnosis_result(session)
        fallback_text = f"📋 Diagnosis: {session.alert.alert_name}"

    try:
        kwargs: dict[str, Any] = {
            "channel": channel,
            "text": fallback_text,
            **message,
        }
        # Reply in thread if we have a parent message
        if session.slack_thread_ts:
            kwargs["thread_ts"] = session.slack_thread_ts

        result = await _slack_post(**kwargs)
        logger.info("Posted diagnosis result for session %s (ts=%s)", session.id, result["ts"])
    except Exception:
        logger.exception("Failed to post diagnosis for session %s", session.id)


async def post_in_thread(session: DiagnosisSession, message: dict[str, Any], text: str) -> None:
    """Post a follow-up message in the session's Slack thread."""
    channel = session.slack_channel or settings.slack_channel_id

    try:
        await _slack_post(
            channel=channel,
            text=text,
            thread_ts=session.slack_thread_ts,
            **message,
        )
    except Exception:
        logger.exception("Failed to post thread message for session %s", session.id)


# ---------------------------------------------------------------------------
# Interactive endpoint — button clicks (Approve / Reject / Details)
# ---------------------------------------------------------------------------
@router.post("/interactions")
async def handle_interaction(request: Request) -> Response:
    """Handle Slack interactive component callbacks (button clicks).

    CRITICAL: Slack requires 200 within 3 seconds or it retries.
    We validate the request synchronously and return 200 immediately,
    then process the action in a background task.

    Idempotency: Each action is keyed by trigger_id + action_id.
    Slack retries are silently deduplicated.
    """
    # Rate limit
    if not slack_limiter.allow("interactions"):
        return Response(status_code=429)

    body = await request.body()
    _verify_slack_signature(request, body)

    form_data = parse_qs(body.decode("utf-8"))
    payload_str = form_data.get("payload", [""])[0]
    if not payload_str:
        raise HTTPException(status_code=400, detail="Missing payload")

    payload = json.loads(payload_str)
    actions = payload.get("actions", [])
    user = payload.get("user", {})
    user_id = user.get("id", "unknown")
    user_name = user.get("username", user_id)
    trigger_id = payload.get("trigger_id", "")

    # Schedule all actions as background tasks (non-blocking)
    for action in actions:
        action_id = action.get("action_id", "")

        # Idempotency check — deduplicate Slack retries
        idempotency_key = f"{trigger_id}:{action_id}"
        if not _idempotency.check_and_mark(idempotency_key):
            logger.info("Deduplicated Slack action: %s", idempotency_key)
            continue

        if action_id.startswith("approve:"):
            session_id = action_id.split(":", 1)[1]
            asyncio.create_task(
                _safe_handle("approve", _handle_approve, session_id, user_id, user_name)
            )

        elif action_id.startswith("reject:"):
            session_id = action_id.split(":", 1)[1]
            asyncio.create_task(
                _safe_handle("reject", _handle_reject, session_id, user_id, user_name)
            )

        elif action_id.startswith("rollback:"):
            session_id = action_id.split(":", 1)[1]
            asyncio.create_task(
                _safe_handle("rollback", _handle_rollback, session_id, user_id, user_name)
            )

        elif action_id.startswith("details:"):
            session_id = action_id.split(":", 1)[1]
            asyncio.create_task(
                _safe_handle(
                    "details", _handle_details,
                    session_id, payload.get("channel", {}).get("id"),
                )
            )

        else:
            logger.warning("Unknown Slack action: %s (user=%s)", action_id, user_name)

    # Return 200 immediately — Slack won't retry
    return Response(status_code=200)


async def _safe_handle(name: str, handler, *args) -> None:
    """Wrapper that catches and logs exceptions from background action handlers."""
    try:
        await handler(*args)
    except Exception:
        logger.exception("Background handler '%s' failed (args=%s)", name, args[:2])


async def _handle_approve(session_id: str, user_id: str, user_name: str) -> None:
    """Process fix approval."""
    session = session_store.get(session_id)
    if not session:
        logger.warning("Approve: session %s not found", session_id)
        return

    if session.phase != SessionPhase.AWAITING_APPROVAL:
        logger.warning(
            "Approve: session %s in phase %s, expected AWAITING_APPROVAL",
            session_id, session.phase.value,
        )
        return

    # RBAC check — verify user is authorized to approve at this risk level
    from ..agent.rbac import approval_policy, AuthzDecision

    risk_level = session.fix_proposal.risk_level if session.fix_proposal else None
    authz = await approval_policy.authorize(user_id, user_name, risk_level)

    if authz.decision == AuthzDecision.DENIED:
        await post_in_thread(
            session,
            {"blocks": [{"type": "section", "text": {
                "type": "mrkdwn", "text": authz.to_slack_text(),
            }}]},
            "Authorization denied",
        )
        return

    if authz.decision == AuthzDecision.NEEDS_SENIOR:
        await post_in_thread(
            session,
            {"blocks": [{"type": "section", "text": {
                "type": "mrkdwn", "text": authz.to_slack_text(),
            }}]},
            "Senior approval needed",
        )
        return

    session.approve(user_name)
    logger.info("Session %s approved by %s", session_id, user_name)

    # Acknowledge incidents on PagerDuty/OpsGenie (human is responding)
    from ..notifications.base import incident_router
    if hasattr(session, "incident_ids") and session.incident_ids:
        await incident_router.acknowledge_all(session.incident_ids)

    # Post confirmation
    message = format_approval_confirmation(session, user_id)
    await post_in_thread(session, message, f"✅ Fix approved by {user_name}")

    # Execute the fix via the execution engine
    from ..agent.executor import FixExecutor
    from ..agent.guardrails import evaluate_guardrails

    # Pre-flight guardrail check
    guardrail_result = evaluate_guardrails(session)
    if not guardrail_result.passed:
        session.fail(f"Guardrails blocked: {guardrail_result.summary()}")
        await post_in_thread(
            session,
            {"blocks": [{"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"❌ *Execution blocked by guardrails:*\n{guardrail_result.summary()}"
            }}]},
            "Execution blocked",
        )
        return

    # Post guardrail warnings if any
    if guardrail_result.warnings:
        warning_text = "\n".join(f"  • {w}" for w in guardrail_result.warnings)
        await post_in_thread(
            session,
            {"blocks": [{"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"⚠️ *Guardrail warnings:*\n{warning_text}\n\nProceeding with execution..."
            }}]},
            "Guardrail warnings",
        )

    # Execute
    await post_in_thread(
        session,
        {"blocks": [{"type": "section", "text": {
            "type": "mrkdwn",
            "text": "🔄 *Executing fix...* This may take a moment."
        }}]},
        "Executing fix...",
    )

    try:
        executor = FixExecutor()
        exec_result = await executor.execute(session)

        # Post execution result
        result_blocks = [
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": exec_result.to_slack_text(),
            }},
        ]

        if exec_result.needs_rollback:
            result_blocks.append({
                "type": "actions",
                "block_id": f"rollback-{session_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Execute Rollback", "emoji": True},
                        "action_id": f"rollback:{session_id}",
                        "style": "danger",
                    }
                ],
            })

        await post_in_thread(session, {"blocks": result_blocks}, "Execution result")

        # Resolve incidents on PagerDuty/OpsGenie
        if hasattr(session, "incident_ids") and session.incident_ids:
            if exec_result.success:
                await incident_router.resolve_all(
                    session.incident_ids,
                    note=f"Fix applied: {exec_result.summary}",
                )
            else:
                await incident_router.add_note_all(
                    session.incident_ids,
                    f"Execution failed: {exec_result.summary}",
                )

    except Exception as e:
        logger.exception("Executor failed for session %s", session_id)
        session.fail(f"Executor error: {e}")
        await post_in_thread(
            session,
            {"blocks": [{"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"❌ *Execution failed:*\n```{str(e)[:1000]}```"
            }}]},
            "Execution failed",
        )
        # Add note to incidents about the failure
        if hasattr(session, "incident_ids") and session.incident_ids:
            await incident_router.add_note_all(
                session.incident_ids,
                f"Executor error: {e}",
            )


async def _handle_reject(session_id: str, user_id: str, user_name: str) -> None:
    """Process fix rejection."""
    session = session_store.get(session_id)
    if not session:
        logger.warning("Reject: session %s not found", session_id)
        return

    session.reject(user_name)
    logger.info("Session %s rejected by %s", session_id, user_name)

    message = format_rejection(session, user_id)
    await post_in_thread(session, message, f"❌ Fix rejected by {user_name}")

    # Resolve incidents on PagerDuty/OpsGenie with rejection note
    from ..notifications.base import incident_router
    if hasattr(session, "incident_ids") and session.incident_ids:
        await incident_router.resolve_all(
            session.incident_ids,
            note=f"Fix rejected by {user_name} — manual investigation required",
        )


async def _handle_rollback(session_id: str, user_id: str, user_name: str) -> None:
    """Execute the rollback plan for a failed fix."""
    session = session_store.get(session_id)
    if not session:
        logger.warning("Rollback: session %s not found", session_id)
        return

    if not session.fix_proposal or not session.fix_proposal.rollback_plan:
        await post_in_thread(
            session,
            {"blocks": [{"type": "section", "text": {
                "type": "mrkdwn",
                "text": "❌ No rollback plan available for this fix."
            }}]},
            "Rollback unavailable",
        )
        return

    await post_in_thread(
        session,
        {"blocks": [{"type": "section", "text": {
            "type": "mrkdwn",
            "text": f"🔄 *Rolling back fix...* (requested by <@{user_id}>)"
        }}]},
        "Rollback starting",
    )

    # Use the executor to run the rollback
    from ..agent.executor import FixExecutor

    try:
        executor = FixExecutor()

        # Check if rollback plan mentions rollout undo
        rollback_plan = session.fix_proposal.rollback_plan
        deployment_name = session.alert.labels.get("deployment") or session.alert.labels.get("app", "")
        namespace = session.alert.namespace

        if "rollout undo" in rollback_plan.lower() and deployment_name:
            # Use the rollback_deployment tool directly
            result = await executor.registry.dispatch("rollback_deployment", {
                "namespace": namespace,
                "name": deployment_name,
                "dry_run": "false",
            })
            result_text = result.get("content", [{}])[0].get("text", "No output")
            is_error = result.get("is_error", False)
        else:
            # Generic rollback — log the plan for manual execution
            result_text = f"Rollback plan:\n{rollback_plan}\n\nAutomatic rollback not supported for this plan — please execute manually."
            is_error = False

        emoji = "❌" if is_error else "✅"
        await post_in_thread(
            session,
            {"blocks": [{"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *Rollback Result:*\n```{result_text[:1500]}```"
            }}]},
            "Rollback result",
        )

        logger.info("Rollback executed for session %s by %s: error=%s", session_id, user_name, is_error)

    except Exception as e:
        logger.exception("Rollback failed for session %s", session_id)
        await post_in_thread(
            session,
            {"blocks": [{"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"❌ *Rollback failed:*\n```{str(e)[:1000]}```"
            }}]},
            "Rollback failed",
        )


async def _handle_details(session_id: str, channel_id: str | None) -> None:
    """Post session details in the channel."""
    session = session_store.get(session_id)
    if not session:
        logger.warning("Details: session %s not found", session_id)
        return

    client = _get_slack_client()
    message = format_session_details(session)

    try:
        client.chat_postMessage(
            channel=channel_id or session.slack_channel or settings.slack_channel_id,
            text=f"Session details: {session_id}",
            thread_ts=session.slack_thread_ts,
            **message,
        )
    except Exception:
        logger.exception("Failed to post details for session %s", session_id)


# ---------------------------------------------------------------------------
# Slash command endpoint — /k8s-diag
# ---------------------------------------------------------------------------
@router.post("/commands")
async def handle_slash_command(request: Request) -> dict[str, Any]:
    """Handle /k8s-diag slash commands.

    Usage:
      /k8s-diag status           — list active sessions
      /k8s-diag details <id>     — show session details
      /k8s-diag history          — show recent sessions
    """
    body = await request.body()
    _verify_slack_signature(request, body)

    form_data = parse_qs(body.decode("utf-8"))
    text = form_data.get("text", [""])[0].strip()
    parts = text.split(maxsplit=1)
    command = parts[0].lower() if parts else "status"
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command == "status":
        return _cmd_status()
    elif command == "details" and arg:
        return _cmd_details(arg)
    elif command == "history":
        return _cmd_history()
    else:
        return {
            "response_type": "ephemeral",
            "text": (
                "*Usage:*\n"
                "• `/k8s-diag status` — list active investigations\n"
                "• `/k8s-diag details <session-id>` — show full details\n"
                "• `/k8s-diag history` — show recent sessions"
            ),
        }


def _cmd_status() -> dict[str, Any]:
    """List active diagnosis sessions."""
    active = session_store.active_sessions()
    if not active:
        return {"response_type": "ephemeral", "text": "No active investigations."}

    lines = [f"*Active investigations ({len(active)}):*\n"]
    for s in active:
        pod_info = f" | Pod: `{s.alert.pod}`" if s.alert.pod else ""
        lines.append(
            f"• `{s.id}` — {s.alert.alert_name} in `{s.alert.namespace}`{pod_info} "
            f"({s.phase.value})"
        )

    return {"response_type": "ephemeral", "text": "\n".join(lines)}


def _cmd_details(session_id: str) -> dict[str, Any]:
    """Show session details."""
    session = session_store.get(session_id)
    if not session:
        return {"response_type": "ephemeral", "text": f"Session `{session_id}` not found."}

    return {"response_type": "ephemeral", **format_session_details(session)}


def _cmd_history() -> dict[str, Any]:
    """Show recent sessions."""
    all_sessions = session_store.all_sessions()
    if not all_sessions:
        return {"response_type": "ephemeral", "text": "No sessions recorded yet."}

    # Show last 10
    recent = sorted(all_sessions, key=lambda s: s.created_at, reverse=True)[:10]
    lines = [f"*Recent sessions ({len(recent)} of {len(all_sessions)}):*\n"]
    for s in recent:
        status_emoji = {
            SessionPhase.RESOLVED: ":white_check_mark:",
            SessionPhase.ESCALATED: ":sos:",
            SessionPhase.FAILED: ":x:",
            SessionPhase.AWAITING_APPROVAL: ":hourglass_flowing_sand:",
        }.get(s.phase, ":mag:")

        lines.append(
            f"• {status_emoji} `{s.id}` — {s.alert.alert_name} "
            f"({s.phase.value}, {s.tool_calls_made} tools, {s.total_tokens_used:,} tokens)"
        )

    return {"response_type": "ephemeral", "text": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Slack request signature verification
# ---------------------------------------------------------------------------
def _verify_slack_signature(request: Request, body: bytes) -> None:
    """Verify the Slack request signature to prevent forgery."""
    signing_secret = settings.slack_signing_secret
    if not signing_secret:
        return  # Skip in dev mode

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Slack signature headers")

    # Reject requests older than 5 minutes (replay protection)
    if abs(time.time() - int(timestamp)) > 300:
        raise HTTPException(status_code=401, detail="Request too old")

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    computed = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(computed, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
