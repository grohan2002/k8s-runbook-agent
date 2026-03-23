"""Shared fixtures for the K8s Runbook Agent test suite."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from k8s_runbook_agent.agent.session import DiagnosisSession, SessionStore, session_store
from k8s_runbook_agent.models import (
    AlertStatus,
    Confidence,
    GrafanaAlert,
    RiskLevel,
)
from k8s_runbook_agent.server import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_alert() -> GrafanaAlert:
    """A typical CrashLoopBackOff alert."""
    return GrafanaAlert(
        alert_name="KubePodCrashLooping",
        status=AlertStatus.FIRING,
        labels={
            "namespace": "production",
            "pod": "api-server-abc123",
            "severity": "critical",
            "alertname": "KubePodCrashLooping",
            "container": "api",
        },
        annotations={
            "summary": "Pod production/api-server-abc123 is crash looping",
            "description": "Pod has restarted 5 times in the last 10 minutes",
        },
        fingerprint="fp-crash-001",
    )


@pytest.fixture
def sample_oom_alert() -> GrafanaAlert:
    """An OOMKilled alert."""
    return GrafanaAlert(
        alert_name="KubePodOOMKilled",
        status=AlertStatus.FIRING,
        labels={
            "namespace": "staging",
            "pod": "worker-xyz789",
            "severity": "warning",
        },
        fingerprint="fp-oom-001",
    )


@pytest.fixture
def sample_session(sample_alert) -> DiagnosisSession:
    """A session that has been diagnosed with a fix proposal."""
    session = DiagnosisSession(sample_alert)
    session.set_diagnosis(
        root_cause="Container OOMKilled — memory limit 256Mi is too low for workload",
        confidence=Confidence.HIGH,
        evidence=[
            "Last termination reason: OOMKilled",
            "Exit code 137",
            "Memory usage 254Mi / 256Mi limit",
        ],
        ruled_out=["Image pull failure", "Liveness probe misconfiguration"],
    )
    session.set_fix_proposal(
        summary="Increase memory limit from 256Mi to 512Mi",
        description="Patch deployment api-server to set resources.limits.memory=512Mi",
        risk_level=RiskLevel.LOW,
        dry_run_output="spec.containers[0].resources.limits.memory: 256Mi → 512Mi",
        rollback_plan="kubectl rollout undo deployment/api-server -n production",
    )
    return session


@pytest.fixture
def clean_session_store():
    """Reset the session store between tests."""
    original = session_store._sessions.copy()
    session_store._sessions.clear()
    yield session_store
    session_store._sessions = original


@pytest.fixture
async def async_client():
    """HTTPX async client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def grafana_webhook_payload() -> dict:
    """A realistic Grafana unified alerting webhook payload."""
    return {
        "receiver": "k8s-runbook-agent",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "namespace": "production",
                    "pod": "api-server-abc123",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "Pod is crash looping",
                    "description": "Restarted 5 times",
                },
                "startsAt": "2026-03-21T10:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://grafana:3000/alerting/123",
                "fingerprint": "fp-test-001",
            }
        ],
        "groupLabels": {"alertname": "KubePodCrashLooping"},
        "commonLabels": {"severity": "critical"},
        "externalURL": "http://grafana:3000",
    }
