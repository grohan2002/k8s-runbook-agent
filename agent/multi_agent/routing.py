"""Deterministic alert routing — maps alerts to specialist domains.

Two routing tiers:
  1. Alert name regex matching (fast, high confidence)
  2. Label-based inference (fallback when name doesn't match)

The triage agent (Haiku) can override this routing with nuanced classification,
but this table serves as the fallback when Haiku is unavailable or low confidence.
"""

from __future__ import annotations

import re
from typing import Any

from ...models import GrafanaAlert
from .tool_subsets import SpecialistDomain

# Alert name patterns → specialist domain
# Order matters — first match wins
ALERT_ROUTING_TABLE: list[tuple[str, SpecialistDomain]] = [
    # Pod-level alerts
    (r"(?i)CrashLoop|OOMKill|ImagePull|Evict|Unschedul|NotSchedul|PodFail|PodPending|PodNotReady", SpecialistDomain.POD),
    # Network alerts
    (r"(?i)DNS|Ingress|NoEndpoint|Endpoint|Certificate|TLS|SSL|NetworkPolic|CoreDNS", SpecialistDomain.NETWORK),
    # Infrastructure alerts
    (r"(?i)NodeNotReady|NodeUnreach|CPUThrottl|HPAMax|PVC|PersistentVolume|DiskPressure|MemoryPressure|PIDPressure", SpecialistDomain.INFRASTRUCTURE),
    # Application / deployment alerts
    (r"(?i)ErrorRate|Latency|FailedRollout|ReplicasMismatch|JobFail|JobNotComplete|5xx|Deployment", SpecialistDomain.APPLICATION),
]


def route_by_alert_name(alert_name: str) -> SpecialistDomain | None:
    """Match alert name against routing table. Returns None if no match."""
    for pattern, domain in ALERT_ROUTING_TABLE:
        if re.search(pattern, alert_name):
            return domain
    return None


def route_by_labels(alert: GrafanaAlert) -> SpecialistDomain:
    """Infer specialist domain from alert labels. Always returns a domain."""
    labels = alert.labels

    # Node-level indicators
    if labels.get("node") or labels.get("instance"):
        if not labels.get("pod"):
            return SpecialistDomain.INFRASTRUCTURE

    # Network indicators
    if labels.get("ingress") or labels.get("service"):
        return SpecialistDomain.NETWORK

    # Storage indicators
    if labels.get("persistentvolumeclaim") or labels.get("pvc"):
        return SpecialistDomain.INFRASTRUCTURE

    # Pod-level (most common)
    if alert.pod:
        return SpecialistDomain.POD

    # Default to application (broadest scope)
    return SpecialistDomain.APPLICATION


def route_alert(alert: GrafanaAlert) -> SpecialistDomain:
    """Deterministic routing: try alert name first, then labels."""
    domain = route_by_alert_name(alert.alert_name)
    if domain:
        return domain
    return route_by_labels(alert)
