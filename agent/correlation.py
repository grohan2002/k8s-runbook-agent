"""Alert correlation — group related alerts into a single investigation.

When multiple alerts fire for the same workload (e.g., CrashLoopBackOff and
HighRestartRate for the same deployment), running separate investigations
wastes resources and confuses the operator.

Correlation strategy:
  1. Workload key: (namespace, deployment/statefulset name)
  2. Time window: alerts within CORRELATION_WINDOW_SECONDS of each other
  3. The first alert starts the investigation; subsequent correlated alerts
     are attached to the existing session as additional context.

This module is called by the Grafana webhook handler before starting a new
investigation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any

from ..models import GrafanaAlert
from .session import DiagnosisSession, SessionPhase, session_store

logger = logging.getLogger(__name__)

CORRELATION_WINDOW_SECONDS = 120  # 2-minute window


# ---------------------------------------------------------------------------
# Correlation key extraction
# ---------------------------------------------------------------------------
def _extract_workload_key(alert: GrafanaAlert) -> str | None:
    """Extract a workload identifier from alert labels.

    Returns a normalized key like "namespace/deployment/api-server" or None
    if no workload can be identified.
    """
    ns = alert.namespace
    labels = alert.labels

    # Try common workload label patterns
    for key in ("deployment", "app", "app.kubernetes.io/name", "statefulset", "daemonset"):
        value = labels.get(key, "")
        if value:
            kind = key.split("/")[-1] if "/" in key else key
            if kind == "app.kubernetes.io/name":
                kind = "app"
            return f"{ns}/{kind}/{value}"

    # Fall back to pod name with suffix stripped (pod-abc123 → pod)
    pod = alert.pod
    if pod:
        # Strip the random suffix from pod names (deployment-pod-xxxxx)
        parts = pod.rsplit("-", 2)
        if len(parts) >= 2:
            base = "-".join(parts[:-1]) if len(parts[-1]) <= 10 else pod
        else:
            base = pod
        return f"{ns}/pod/{base}"

    return None


def _extract_node_key(alert: GrafanaAlert) -> str | None:
    """Extract a node identifier for node-level alerts."""
    node = alert.labels.get("node", alert.labels.get("instance", ""))
    if node:
        return f"node/{node}"
    return None


# ---------------------------------------------------------------------------
# Correlator
# ---------------------------------------------------------------------------
@dataclass
class CorrelatedAlert:
    """An alert that was correlated to an existing session."""

    alert: GrafanaAlert
    correlation_key: str
    correlated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AlertCorrelator:
    """Correlates incoming alerts to existing active sessions.

    Usage:
        correlator = AlertCorrelator()

        # Check if an alert should join an existing investigation
        session = correlator.correlate(alert)
        if session:
            # Alert was correlated — don't start a new investigation
            pass
        else:
            # New alert — start investigation
            pass
    """

    def __init__(self, window_seconds: int = CORRELATION_WINDOW_SECONDS) -> None:
        self.window = timedelta(seconds=window_seconds)
        # Track workload keys → session IDs for fast lookup
        self._key_to_session: dict[str, str] = {}
        self._session_correlated: dict[str, list[CorrelatedAlert]] = {}

    def correlate(self, alert: GrafanaAlert) -> DiagnosisSession | None:
        """Check if this alert should be correlated to an existing session.

        Returns the existing session if correlated, None if this is a new alert.
        Side effect: registers the correlation key for future lookups.
        """
        now = datetime.now(timezone.utc)

        # Extract correlation keys (workload + node)
        keys = []
        wk = _extract_workload_key(alert)
        if wk:
            keys.append(wk)
        nk = _extract_node_key(alert)
        if nk:
            keys.append(nk)

        if not keys:
            return None

        # Check each key against active sessions
        for key in keys:
            session_id = self._key_to_session.get(key)
            if not session_id:
                continue

            session = session_store.get(session_id)
            if not session:
                # Session was cleaned up
                del self._key_to_session[key]
                continue

            # Verify session is still active and within time window
            if session.phase in (SessionPhase.RESOLVED, SessionPhase.FAILED, SessionPhase.ESCALATED):
                del self._key_to_session[key]
                continue

            age = now - session.created_at
            if age > self.window:
                # Session is too old — allow new investigation
                continue

            # Correlate!
            correlated = CorrelatedAlert(alert=alert, correlation_key=key)
            self._session_correlated.setdefault(session_id, []).append(correlated)

            # Add context to the session so Claude sees the additional alert
            session.add_user_message(
                f"\n[CORRELATED ALERT] Additional alert fired for the same workload:\n"
                f"  Alert: {alert.alert_name}\n"
                f"  Severity: {alert.severity}\n"
                f"  Summary: {alert.summary}\n"
                f"  Correlation key: {key}\n"
                f"\nThis is likely related to the issue you're already investigating. "
                f"Factor this into your diagnosis."
            )

            logger.info(
                "Correlated alert %s to session %s (key=%s, total=%d correlated)",
                alert.alert_name, session_id, key,
                len(self._session_correlated[session_id]),
            )
            return session

        return None

    def register_session(self, session: DiagnosisSession) -> None:
        """Register a new session's correlation keys for future lookups."""
        keys = []
        wk = _extract_workload_key(session.alert)
        if wk:
            keys.append(wk)
        nk = _extract_node_key(session.alert)
        if nk:
            keys.append(nk)

        for key in keys:
            self._key_to_session[key] = session.id

        if keys:
            logger.debug("Registered correlation keys for %s: %s", session.id, keys)

    def get_correlated_alerts(self, session_id: str) -> list[CorrelatedAlert]:
        """Get all alerts that were correlated to a session."""
        return self._session_correlated.get(session_id, [])

    def cleanup(self) -> None:
        """Remove entries for sessions that are no longer active."""
        active_ids = {s.id for s in session_store.active_sessions()}
        stale_keys = [k for k, sid in self._key_to_session.items() if sid not in active_ids]
        for key in stale_keys:
            del self._key_to_session[key]

        stale_sessions = [sid for sid in self._session_correlated if sid not in active_ids]
        for sid in stale_sessions:
            del self._session_correlated[sid]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
alert_correlator = AlertCorrelator()
