"""OpsGenie (Atlassian) incident management provider.

Uses the OpsGenie Alert API v2 for alert lifecycle management.

Setup:
  1. In OpsGenie: Settings → Integrations → Add Integration → API
  2. Copy the API Key
  3. Set OPSGENIE_API_KEY in env
  4. Optionally set OPSGENIE_TEAM (team name for alert routing)

API Reference: https://docs.opsgenie.com/docs/alert-api
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

# OpsGenie has separate US and EU endpoints
OPSGENIE_API_URLS = {
    "us": "https://api.opsgenie.com/v2/alerts",
    "eu": "https://api.eu.opsgenie.com/v2/alerts",
}


class OpsGenieProvider(IncidentProvider):
    """OpsGenie integration via Alert API v2.

    Features:
      - Creates alerts with priority mapping (P1-P5)
      - Routes to specified team
      - Links to Slack thread for investigation context
      - Acknowledges alerts when human approves/rejects
      - Closes alerts when fix is applied
      - Adds notes with diagnosis/execution details
    """

    def __init__(
        self,
        api_key: str = "",
        team: str = "",
        region: str = "",
    ) -> None:
        self._api_key = api_key or getattr(settings, "opsgenie_api_key", "")
        self._team = team or getattr(settings, "opsgenie_team", "")
        effective_region = region or getattr(settings, "opsgenie_region", "us")
        self._base_url = OPSGENIE_API_URLS.get(effective_region, OPSGENIE_API_URLS["us"])

    @property
    def name(self) -> str:
        return "opsgenie"

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def create_incident(self, ctx: IncidentContext) -> str | None:
        """Create an OpsGenie alert."""
        priority = self._map_priority(ctx.urgency)

        payload: dict[str, Any] = {
            "message": ctx.title[:130],  # OpsGenie limit is 130 chars
            "alias": f"k8s-runbook-{ctx.session_id}",
            "description": ctx.description[:15000],  # OpsGenie limit
            "priority": priority,
            "source": "k8s-runbook-agent",
            "tags": ["kubernetes", ctx.alert_name, ctx.namespace, f"severity:{ctx.severity}"],
            "details": {
                "session_id": ctx.session_id,
                "alert_name": ctx.alert_name,
                "namespace": ctx.namespace,
                "pod": ctx.pod,
                "severity": ctx.severity,
                "fix_summary": ctx.fix_summary,
                "risk_level": ctx.risk_level,
                "phase": ctx.custom_details.get("phase", ""),
                "tool_calls": str(ctx.custom_details.get("tool_calls", 0)),
                "tokens_used": str(ctx.custom_details.get("tokens_used", 0)),
            },
            "entity": f"{ctx.namespace}/{ctx.pod}" if ctx.pod else ctx.namespace,
        }

        # Route to team if configured
        if self._team:
            payload["responders"] = [{"name": self._team, "type": "team"}]

        result = await self._api_request("POST", "", payload)
        if result:
            request_id = result.get("requestId", "")
            logger.info(
                "OpsGenie alert created: alias=k8s-runbook-%s, requestId=%s, priority=%s",
                ctx.session_id, request_id, priority,
            )
            return f"k8s-runbook-{ctx.session_id}"

        return None

    async def acknowledge_incident(self, incident_id: str) -> bool:
        """Acknowledge an OpsGenie alert."""
        payload = {
            "source": "k8s-runbook-agent",
            "note": "Human reviewing the proposed fix via Slack",
        }
        result = await self._api_request(
            "POST",
            f"/{incident_id}/acknowledge?identifierType=alias",
            payload,
        )
        if result:
            logger.info("OpsGenie alert acknowledged: %s", incident_id)
            return True
        return False

    async def resolve_incident(self, incident_id: str, note: str = "") -> bool:
        """Close an OpsGenie alert."""
        payload: dict[str, Any] = {
            "source": "k8s-runbook-agent",
        }
        if note:
            payload["note"] = note[:25000]  # OpsGenie limit

        result = await self._api_request(
            "POST",
            f"/{incident_id}/close?identifierType=alias",
            payload,
        )
        if result:
            logger.info("OpsGenie alert closed: %s", incident_id)
            return True
        return False

    async def add_note(self, incident_id: str, note: str) -> bool:
        """Add a note to an OpsGenie alert."""
        payload = {
            "note": note[:25000],
            "source": "k8s-runbook-agent",
        }
        result = await self._api_request(
            "POST",
            f"/{incident_id}/notes?identifierType=alias",
            payload,
        )
        if result:
            logger.info("OpsGenie note added to %s", incident_id)
            return True
        return False

    def _map_priority(self, urgency: IncidentUrgency) -> str:
        """Map urgency to OpsGenie priority (P1-P5)."""
        return {
            IncidentUrgency.CRITICAL: "P1",
            IncidentUrgency.HIGH: "P2",
            IncidentUrgency.LOW: "P3",
        }.get(urgency, "P3")

    async def _api_request(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> dict | None:
        """Make an async API request to OpsGenie."""
        try:
            return await asyncio.to_thread(
                self._api_request_sync, method, path, payload
            )
        except Exception:
            logger.exception("OpsGenie API call failed")
            return None

    def _api_request_sync(
        self, method: str, path: str, payload: dict[str, Any]
    ) -> dict | None:
        """Synchronous HTTP request to OpsGenie (run in thread pool)."""
        url = f"{self._base_url}{path}"
        data = json.dumps(payload).encode("utf-8")

        req = Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"GenieKey {self._api_key}",
            },
            method=method,
        )

        try:
            with urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
                return body
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error("OpsGenie API error %d: %s", e.code, body[:500])
            return None
        except URLError as e:
            logger.error("OpsGenie connection error: %s", e.reason)
            return None
