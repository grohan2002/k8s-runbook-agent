"""Incident management provider interface.

All providers implement the same IncidentProvider protocol so they can be
composed via the IncidentRouter.  The router fans out escalation events to
every enabled provider (Slack, PagerDuty, OpsGenie, or custom).

Design:
  - Providers are stateless — they map session data to provider API calls.
  - Each provider stores its incident ID back on the session so we can
    acknowledge/resolve later.
  - All providers are async and tolerant of failures (log + continue).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..agent.session import DiagnosisSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Incident urgency mapping
# ---------------------------------------------------------------------------
class IncidentUrgency(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    LOW = "low"


def session_to_urgency(session: DiagnosisSession) -> IncidentUrgency:
    """Map an alert's severity to an incident urgency level."""
    severity = session.alert.severity.lower()
    if severity == "critical":
        return IncidentUrgency.CRITICAL
    elif severity in ("warning", "error"):
        return IncidentUrgency.HIGH
    return IncidentUrgency.LOW


# ---------------------------------------------------------------------------
# Incident data
# ---------------------------------------------------------------------------
@dataclass
class IncidentContext:
    """Normalized incident data passed to all providers."""

    session_id: str
    title: str
    description: str
    urgency: IncidentUrgency
    source: str = "k8s-runbook-agent"
    alert_name: str = ""
    namespace: str = ""
    pod: str = ""
    severity: str = ""
    fix_summary: str = ""
    risk_level: str = ""
    slack_thread_url: str = ""
    custom_details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_session(cls, session: DiagnosisSession, message: str = "") -> IncidentContext:
        """Build incident context from a diagnosis session."""
        alert = session.alert
        fix = session.fix_proposal

        title = f"[{alert.severity.upper()}] {alert.alert_name} in {alert.namespace}"
        if alert.pod:
            title += f" ({alert.pod})"

        description_parts = [message] if message else []
        if session.diagnosis:
            description_parts.append(f"Root Cause: {session.diagnosis.root_cause}")
            description_parts.append(f"Confidence: {session.diagnosis.confidence.value}")
        if fix:
            description_parts.append(f"Proposed Fix: {fix.summary}")
            description_parts.append(f"Risk Level: {fix.risk_level.value}")
        description_parts.append(f"Session ID: {session.id}")

        slack_url = ""
        if session.slack_channel and session.slack_thread_ts:
            slack_url = f"https://slack.com/archives/{session.slack_channel}/p{session.slack_thread_ts.replace('.', '')}"

        return cls(
            session_id=session.id,
            title=title,
            description="\n".join(description_parts),
            urgency=session_to_urgency(session),
            alert_name=alert.alert_name,
            namespace=alert.namespace,
            pod=alert.pod or "",
            severity=alert.severity,
            fix_summary=fix.summary if fix else "",
            risk_level=fix.risk_level.value if fix else "",
            slack_thread_url=slack_url,
            custom_details={
                "tool_calls": session.tool_calls_made,
                "tokens_used": session.total_tokens_used,
                "phase": session.phase.value,
            },
        )


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------
class IncidentProvider(ABC):
    """Base class for incident management providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging and config (e.g. 'pagerduty', 'opsgenie')."""
        ...

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether this provider is configured and ready to use."""
        ...

    @abstractmethod
    async def create_incident(self, ctx: IncidentContext) -> str | None:
        """Create an incident. Returns the provider's incident ID, or None on failure."""
        ...

    @abstractmethod
    async def acknowledge_incident(self, incident_id: str) -> bool:
        """Acknowledge an incident (human is looking at it). Returns success."""
        ...

    @abstractmethod
    async def resolve_incident(self, incident_id: str, note: str = "") -> bool:
        """Resolve/close an incident. Returns success."""
        ...

    async def add_note(self, incident_id: str, note: str) -> bool:
        """Add a note/comment to an existing incident. Optional."""
        return True  # Default no-op for providers that don't support notes


# ---------------------------------------------------------------------------
# Incident router — fans out to all enabled providers
# ---------------------------------------------------------------------------
class IncidentRouter:
    """Routes incident lifecycle events to all registered providers.

    Usage:
        router = IncidentRouter()
        router.register(PagerDutyProvider(...))
        router.register(OpsGenieProvider(...))

        incident_ids = await router.create_incident(ctx)
        # incident_ids = {"pagerduty": "PD123", "opsgenie": "OG456"}
    """

    def __init__(self) -> None:
        self._providers: list[IncidentProvider] = []

    def register(self, provider: IncidentProvider) -> None:
        if provider.enabled:
            self._providers.append(provider)
            logger.info("Incident provider registered: %s", provider.name)
        else:
            logger.info("Incident provider skipped (not configured): %s", provider.name)

    @property
    def enabled_providers(self) -> list[str]:
        return [p.name for p in self._providers]

    @property
    def has_providers(self) -> bool:
        return len(self._providers) > 0

    async def create_incident(self, ctx: IncidentContext) -> dict[str, str]:
        """Create incident on all providers. Returns {provider_name: incident_id}."""
        results: dict[str, str] = {}
        for provider in self._providers:
            try:
                incident_id = await provider.create_incident(ctx)
                if incident_id:
                    results[provider.name] = incident_id
                    logger.info(
                        "Incident created on %s: %s (session=%s)",
                        provider.name, incident_id, ctx.session_id,
                    )
            except Exception:
                logger.exception("Failed to create incident on %s for session %s", provider.name, ctx.session_id)
        return results

    async def acknowledge_all(self, incident_ids: dict[str, str]) -> None:
        """Acknowledge incident on all providers."""
        for provider in self._providers:
            iid = incident_ids.get(provider.name)
            if iid:
                try:
                    await provider.acknowledge_incident(iid)
                except Exception:
                    logger.exception("Failed to ack on %s: %s", provider.name, iid)

    async def resolve_all(self, incident_ids: dict[str, str], note: str = "") -> None:
        """Resolve incident on all providers."""
        for provider in self._providers:
            iid = incident_ids.get(provider.name)
            if iid:
                try:
                    await provider.resolve_incident(iid, note=note)
                except Exception:
                    logger.exception("Failed to resolve on %s: %s", provider.name, iid)

    async def add_note_all(self, incident_ids: dict[str, str], note: str) -> None:
        """Add a note to incident on all providers."""
        for provider in self._providers:
            iid = incident_ids.get(provider.name)
            if iid:
                try:
                    await provider.add_note(iid, note)
                except Exception:
                    logger.exception("Failed to add note on %s: %s", provider.name, iid)


# Module-level singleton
incident_router = IncidentRouter()
