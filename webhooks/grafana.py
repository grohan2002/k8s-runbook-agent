"""Grafana webhook receiver.

Grafana sends alerts as POST requests to a "contact point" webhook.
This module:
  1. Validates the webhook secret (Authorization header)
  2. Parses the Grafana alert payload into our GrafanaAlert model
  3. Deduplicates alerts (same fingerprint → skip if already investigating)
  4. Kicks off the diagnostic orchestrator in the background
  5. Posts the initial Slack notification

Grafana alert payload structure (v9+ unified alerting):
{
  "receiver": "...",
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {"alertname": "...", "namespace": "...", "pod": "..."},
      "annotations": {"summary": "...", "description": "..."},
      "startsAt": "2025-01-15T10:00:00Z",
      "endsAt": "0001-01-01T00:00:00Z",
      "generatorURL": "...",
      "fingerprint": "abc123"
    }
  ],
  ...
}
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ..agent.orchestrator import DiagnosticOrchestrator
from ..agent.session import session_store
from ..config import settings
from ..models import AlertStatus, GrafanaAlert
from ..observability.logging import set_session_context
from ..observability.metrics import alerts_received, active_sessions
from ..observability.rate_limit import webhook_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Module-level orchestrator (reuses tool registry across requests)
_orchestrator = None


def get_orchestrator():
    """Get the orchestrator — multi-agent or single-agent based on config."""
    global _orchestrator
    if _orchestrator is None:
        from ..config import settings

        if settings.multi_agent_enabled:
            from ..agent.multi_agent import MultiAgentOrchestrator

            _orchestrator = MultiAgentOrchestrator(
                fallback=DiagnosticOrchestrator(),
            )
            logger.info("Using multi-agent orchestrator (triage → specialist → coordinator)")
        else:
            _orchestrator = DiagnosticOrchestrator()
            logger.info("Using single-agent orchestrator")
    return _orchestrator


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------
@router.post("/grafana")
async def receive_grafana_alert(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Receive a Grafana webhook alert and start diagnosis."""

    # 0. Rate limit
    if not webhook_limiter.allow("grafana"):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # 1. Validate webhook secret
    _validate_secret(authorization)

    # 2. Parse the raw payload
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON in webhook")
    alerts = _parse_grafana_payload(body)

    if not alerts:
        return {"status": "ignored", "reason": "no firing alerts in payload"}

    # 3. Process each firing alert
    from ..agent.correlation import alert_correlator

    sessions_started = []
    for alert in alerts:
        # Deduplicate: skip if already investigating this fingerprint
        if alert.fingerprint:
            existing = session_store.get_by_fingerprint(alert.fingerprint)
            if existing:
                logger.info(
                    "Skipping duplicate alert %s (fingerprint=%s, existing session=%s)",
                    alert.alert_name,
                    alert.fingerprint,
                    existing.id,
                )
                continue

        # Correlation: check if this alert belongs to an existing investigation
        correlated_session = alert_correlator.correlate(alert)
        if correlated_session:
            logger.info(
                "Correlated alert %s to existing session %s",
                alert.alert_name, correlated_session.id,
            )
            continue

        # 4. Check session limit before starting new investigation
        from ..security import check_session_limit

        try:
            check_session_limit()
        except Exception as limit_err:
            logger.warning("Session limit reached, skipping alert %s: %s", alert.alert_name, limit_err)
            continue

        # 5. Record metric + start investigation in the background
        alerts_received.inc({
            "alert_name": alert.alert_name,
            "severity": alert.severity,
            "namespace": alert.namespace,
        })
        session_id = _start_investigation(alert)
        sessions_started.append(session_id)

    return {
        "status": "accepted",
        "sessions_started": sessions_started,
        "alerts_received": len(alerts),
    }


# ---------------------------------------------------------------------------
# Grafana resolved webhook
# ---------------------------------------------------------------------------
@router.post("/grafana/resolved")
async def receive_grafana_resolved(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Handle resolved alerts — update any active sessions."""
    # Rate limit
    if not webhook_limiter.allow("grafana-resolved"):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # Signature verification (same as firing alerts)
    _validate_secret(authorization)

    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON in webhook")

    resolved_count = 0
    for raw_alert in body.get("alerts", []):
        if raw_alert.get("status") != "resolved":
            continue

        fingerprint = raw_alert.get("fingerprint")
        if fingerprint:
            session = session_store.get_by_fingerprint(fingerprint)
            if session:
                session.mark_resolved("Alert auto-resolved by Grafana")
                resolved_count += 1
                logger.info("Session %s auto-resolved by Grafana", session.id)

                # Resolve external incidents (PagerDuty / OpsGenie)
                if hasattr(session, "incident_ids") and session.incident_ids:
                    try:
                        from ..notifications.base import incident_router

                        await incident_router.resolve_all(
                            session.incident_ids,
                            note="Alert auto-resolved by Grafana",
                        )
                    except Exception:
                        logger.warning(
                            "Failed to resolve external incidents for session %s",
                            session.id, exc_info=True,
                        )

    return {"status": "ok", "resolved_count": resolved_count}


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------
def _parse_grafana_payload(body: dict[str, Any]) -> list[GrafanaAlert]:
    """Parse Grafana unified alerting webhook payload into GrafanaAlert objects."""
    alerts: list[GrafanaAlert] = []

    for raw_alert in body.get("alerts", []):
        status_str = raw_alert.get("status", "firing")

        # Only process firing alerts for investigation
        if status_str != "firing":
            continue

        labels = raw_alert.get("labels", {})
        annotations = raw_alert.get("annotations", {})

        alert = GrafanaAlert(
            alert_name=labels.get("alertname", "UnknownAlert"),
            status=AlertStatus.FIRING,
            labels=labels,
            annotations=annotations,
            starts_at=raw_alert.get("startsAt"),
            generator_url=raw_alert.get("generatorURL"),
            fingerprint=raw_alert.get("fingerprint"),
        )

        logger.info(
            "Parsed alert: %s (ns=%s, pod=%s, severity=%s)",
            alert.alert_name,
            alert.namespace,
            alert.pod,
            alert.severity,
        )
        alerts.append(alert)

    return alerts


# ---------------------------------------------------------------------------
# Secret validation
# ---------------------------------------------------------------------------
def _validate_secret(authorization: str | None) -> None:
    """Validate the Grafana webhook secret from the Authorization header."""
    expected = settings.grafana_webhook_secret
    if not expected:
        # No secret configured — skip validation (dev mode)
        return

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    # Grafana sends: Authorization: Bearer <secret>
    token = authorization.removeprefix("Bearer ").strip()

    if not hmac.compare_digest(token, expected):
        logger.warning("Invalid webhook secret received")
        raise HTTPException(status_code=403, detail="Invalid webhook secret")


# ---------------------------------------------------------------------------
# Background investigation launcher
# ---------------------------------------------------------------------------
def _start_investigation(alert: GrafanaAlert) -> str:
    """Kick off the orchestrator in the background and return the session ID."""
    orchestrator = get_orchestrator()

    async def _run() -> None:
        # Set correlation context so all logs in this investigation include session info
        set_session_context(alert_name=alert.alert_name, namespace=alert.namespace)
        active_sessions.inc()
        session = await orchestrator.investigate(alert)
        active_sessions.dec()
        set_session_context(session_id=session.id)

        # Post results to Slack (imported here to avoid circular deps)
        try:
            from ..slack.bot import post_diagnosis_result
            await post_diagnosis_result(session)
        except Exception:
            logger.exception("Failed to post diagnosis to Slack for session %s", session.id)

    # Schedule on the running event loop
    loop = asyncio.get_event_loop()
    task = loop.create_task(_run())
    # We don't have the session ID yet since investigate() creates it,
    # but we return a placeholder to acknowledge the webhook quickly
    # The real session ID will be posted to Slack when investigation completes
    return f"pending-{alert.fingerprint or alert.alert_name}"
