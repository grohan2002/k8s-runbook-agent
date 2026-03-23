"""PagerDuty incident management provider.

Uses the PagerDuty Events API v2 for incident lifecycle management.
This is the preferred API for automated integrations — it doesn't require
a full PagerDuty API key, just a routing key (integration key) from a service.

Setup:
  1. In PagerDuty: create a Service → Integrations → Events API v2
  2. Copy the Integration Key (routing key)
  3. Set PAGERDUTY_ROUTING_KEY in env

API Reference: https://developer.pagerduty.com/docs/events-api-v2/overview/
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from ..config import settings
from .base import IncidentContext, IncidentProvider, IncidentUrgency

logger = logging.getLogger(__name__)

EVENTS_API_URL = "https://events.pagerduty.com/v2/enqueue"


class PagerDutyProvider(IncidentProvider):
    """PagerDuty integration via Events API v2.

    Features:
      - Creates incidents with severity-based urgency mapping
      - Links back to Slack thread for investigation context
      - Acknowledges incidents when a human approves/rejects the fix
      - Resolves incidents when the fix is applied or the alert clears
      - Attaches custom details (diagnosis, fix proposal, metrics)
    """

    def __init__(
        self,
        routing_key: str = "",
        api_key: str = "",
        source: str = "k8s-runbook-agent",
    ) -> None:
        self._routing_key = routing_key or getattr(settings, "pagerduty_routing_key", "")
        self._api_key = api_key or getattr(settings, "pagerduty_api_key", "")
        self._source = source

    @property
    def name(self) -> str:
        return "pagerduty"

    @property
    def enabled(self) -> bool:
        return bool(self._routing_key)

    async def create_incident(self, ctx: IncidentContext) -> str | None:
        """Create a PagerDuty incident via Events API v2."""
        severity = self._map_severity(ctx.urgency)

        payload = {
            "routing_key": self._routing_key,
            "event_action": "trigger",
            "dedup_key": f"k8s-runbook-{ctx.session_id}",
            "payload": {
                "summary": ctx.title,
                "source": self._source,
                "severity": severity,
                "component": ctx.namespace,
                "group": ctx.alert_name,
                "class": "kubernetes",
                "custom_details": {
                    "session_id": ctx.session_id,
                    "alert_name": ctx.alert_name,
                    "namespace": ctx.namespace,
                    "pod": ctx.pod,
                    "severity": ctx.severity,
                    "diagnosis": ctx.description,
                    "fix_summary": ctx.fix_summary,
                    "risk_level": ctx.risk_level,
                    **ctx.custom_details,
                },
            },
            "links": [],
            "images": [],
        }

        # Add Slack thread link if available
        if ctx.slack_thread_url:
            payload["links"].append({
                "href": ctx.slack_thread_url,
                "text": "View investigation in Slack",
            })

        result = await self._send_event(payload)
        if result:
            dedup_key = result.get("dedup_key", f"k8s-runbook-{ctx.session_id}")
            logger.info(
                "PagerDuty incident created: dedup_key=%s, status=%s",
                dedup_key, result.get("status"),
            )
            return dedup_key
        return None

    async def acknowledge_incident(self, incident_id: str) -> bool:
        """Acknowledge a PagerDuty incident (human is responding)."""
        payload = {
            "routing_key": self._routing_key,
            "event_action": "acknowledge",
            "dedup_key": incident_id,
        }
        result = await self._send_event(payload)
        if result:
            logger.info("PagerDuty incident acknowledged: %s", incident_id)
            return True
        return False

    async def resolve_incident(self, incident_id: str, note: str = "") -> bool:
        """Resolve a PagerDuty incident."""
        payload = {
            "routing_key": self._routing_key,
            "event_action": "resolve",
            "dedup_key": incident_id,
        }
        result = await self._send_event(payload)
        if result:
            logger.info("PagerDuty incident resolved: %s", incident_id)
            return True
        return False

    async def add_note(self, incident_id: str, note: str) -> bool:
        """Add a note to a PagerDuty incident via the REST API.

        Requires a full API key (not just routing key).
        Falls back to no-op if no API key is configured.
        """
        if not self._api_key:
            logger.debug("PagerDuty API key not set — skipping note for %s", incident_id)
            return True  # Graceful no-op

        # Events API v2 doesn't support notes — need to look up incident ID
        # via REST API. For now, log the note and skip.
        logger.info("PagerDuty note (dedup=%s): %s", incident_id, note[:200])
        return True

    def _map_severity(self, urgency: IncidentUrgency) -> str:
        """Map our urgency levels to PagerDuty severity."""
        return {
            IncidentUrgency.CRITICAL: "critical",
            IncidentUrgency.HIGH: "error",
            IncidentUrgency.LOW: "warning",
        }.get(urgency, "warning")

    async def _send_event(self, payload: dict[str, Any]) -> dict | None:
        """Send an event to the PagerDuty Events API v2 (async via thread pool)."""
        try:
            result = await asyncio.to_thread(self._send_event_sync, payload)
            return result
        except Exception:
            logger.exception("PagerDuty API call failed")
            return None

    def _send_event_sync(self, payload: dict[str, Any]) -> dict | None:
        """Synchronous HTTP POST to PagerDuty (run in thread pool)."""
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            EVENTS_API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(data)),
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
                return body
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error("PagerDuty API error %d: %s", e.code, body[:500])
            return None
        except URLError as e:
            logger.error("PagerDuty connection error: %s", e.reason)
            return None
