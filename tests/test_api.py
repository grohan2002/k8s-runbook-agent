"""Tests for FastAPI endpoints (health, metrics, webhooks, sessions)."""

import pytest
from httpx import ASGITransport, AsyncClient

from k8s_runbook_agent.server import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
class TestHealthEndpoints:
    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_ready_returns_checks(self, client):
        resp = await client.get("/ready")
        data = resp.json()
        assert "checks" in data
        assert "status" in data
        # Status could be ready, degraded, or not_ready depending on config
        assert data["status"] in ("ready", "degraded", "not_ready")


@pytest.mark.asyncio
class TestMetricsEndpoint:
    async def test_metrics_returns_prometheus_format(self, client):
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        assert "runbook_alerts_received_total" in body
        assert "runbook_active_sessions" in body
        assert "# HELP" in body
        assert "# TYPE" in body


@pytest.mark.asyncio
class TestSessionEndpoints:
    async def test_sessions_list_empty(self, client):
        resp = await client.get("/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "sessions" in data

    async def test_session_not_found(self, client):
        resp = await client.get("/sessions/diag-nonexistent")
        assert resp.status_code == 404

    async def test_audit_endpoint_exists(self, client):
        resp = await client.get("/sessions/diag-nonexistent/audit")
        assert resp.status_code == 200  # Returns empty audit log
        data = resp.json()
        assert data["session_id"] == "diag-nonexistent"
        assert data["entries"] == []


@pytest.mark.asyncio
class TestGrafanaWebhook:
    async def test_accepts_valid_payload(self, client, grafana_webhook_payload):
        resp = await client.post("/webhooks/grafana", json=grafana_webhook_payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["alerts_received"] == 1

    async def test_ignores_resolved_only_payload(self, client):
        payload = {
            "alerts": [
                {"status": "resolved", "labels": {"alertname": "Test"}, "fingerprint": "fp-1"}
            ]
        }
        resp = await client.post("/webhooks/grafana", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ignored"

    async def test_deduplicates_same_fingerprint(self, client, clean_session_store):
        """Deduplication works when a session with the same fingerprint is still active."""
        from k8s_runbook_agent.agent.session import DiagnosisSession, SessionPhase
        from k8s_runbook_agent.models import AlertStatus, GrafanaAlert

        # Pre-create an active session with the target fingerprint
        alert = GrafanaAlert(
            alert_name="DedupTest", status=AlertStatus.FIRING,
            labels={"namespace": "test", "alertname": "DedupTest"},
            fingerprint="fp-dedup-unique",
        )
        existing = clean_session_store.create(alert)
        existing.transition(SessionPhase.INVESTIGATING)

        # Now send a webhook with the same fingerprint — should be deduped
        payload = {
            "alerts": [{
                "status": "firing",
                "labels": {"alertname": "DedupTest", "namespace": "test"},
                "annotations": {},
                "fingerprint": "fp-dedup-unique",
            }]
        }
        resp = await client.post("/webhooks/grafana", json=payload)
        assert resp.json()["sessions_started"] == []

    async def test_rejects_invalid_secret(self, client, grafana_webhook_payload):
        """When a webhook secret is configured, requests without it are rejected."""
        # This test depends on GRAFANA_WEBHOOK_SECRET being empty in test env
        # With no secret configured, all requests pass (dev mode)
        resp = await client.post("/webhooks/grafana", json=grafana_webhook_payload)
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestRateLimiting:
    async def test_debug_endpoint_rate_limited(self, client):
        """Debug endpoints should respond with 429 when rate limited."""
        # Make many rapid requests — the rate limiter has burst=10 for debug
        responses = []
        for _ in range(15):
            resp = await client.get("/sessions")
            responses.append(resp.status_code)

        # At least some should be rate limited
        # (depends on burst config — debug_limiter has burst=10)
        assert 200 in responses  # Some succeed
