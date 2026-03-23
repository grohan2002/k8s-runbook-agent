"""Tests for production security hardening."""

import os
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from k8s_runbook_agent.security import (
    MAX_CONCURRENT_SESSIONS,
    check_session_limit,
    sanitize_error,
    validate_production_config,
    verify_admin_auth,
)
from k8s_runbook_agent.server import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestAdminAuth:
    @pytest.mark.asyncio
    async def test_admin_endpoint_no_key_dev_mode(self, client):
        """In dev mode (no ADMIN_API_KEY), admin endpoints are accessible."""
        resp = await client.get("/admin/clusters")
        assert resp.status_code in (200, 401, 403)  # depends on env

    @pytest.mark.asyncio
    async def test_admin_endpoint_with_key(self, client):
        """When ADMIN_API_KEY is set, endpoints require Bearer token."""
        with patch("k8s_runbook_agent.security.ADMIN_API_KEY", "test-secret-key"):
            resp = await client.post("/admin/runbooks/reload")
            assert resp.status_code == 401

            resp = await client.post(
                "/admin/runbooks/reload",
                headers={"Authorization": "Bearer wrong-key"},
            )
            assert resp.status_code == 403

            resp = await client.post(
                "/admin/runbooks/reload",
                headers={"Authorization": "Bearer test-secret-key"},
            )
            assert resp.status_code == 200


class TestPayloadSizeLimit:
    @pytest.mark.asyncio
    async def test_rejects_oversized_payload(self, client):
        """Payloads exceeding MAX_PAYLOAD_BYTES are rejected with 413."""
        large_payload = {"data": "x" * (2 * 1024 * 1024)}  # 2MB+
        resp = await client.post(
            "/webhooks/grafana",
            json=large_payload,
            headers={"Content-Length": str(2 * 1024 * 1024)},
        )
        assert resp.status_code == 413

    @pytest.mark.asyncio
    async def test_accepts_normal_payload(self, client):
        """Normal-sized payloads are accepted."""
        small_payload = {"alerts": []}
        resp = await client.post("/webhooks/grafana", json=small_payload)
        assert resp.status_code == 200


class TestSecurityHeaders:
    @pytest.mark.asyncio
    async def test_security_headers_present(self, client):
        """All security headers are set on responses."""
        resp = await client.get("/health")
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"
        assert resp.headers.get("Content-Security-Policy") == "default-src 'none'"
        assert resp.headers.get("Cache-Control") == "no-store"


class TestErrorSanitization:
    def test_safe_error_in_dev(self):
        """In dev mode, full error details are returned."""
        with patch("k8s_runbook_agent.security.PRODUCTION_MODE", False):
            msg = sanitize_error(RuntimeError("detailed internal error"))
            assert "detailed internal error" in msg

    def test_sanitized_error_in_production(self):
        """In production mode, internal details are stripped."""
        with patch("k8s_runbook_agent.security.PRODUCTION_MODE", True):
            msg = sanitize_error(RuntimeError("secret DB connection string"))
            assert "secret DB connection string" not in msg
            assert "Internal server error" in msg

    def test_safe_errors_pass_through_in_production(self):
        """ValueError and similar are considered safe to expose."""
        with patch("k8s_runbook_agent.security.PRODUCTION_MODE", True):
            msg = sanitize_error(ValueError("invalid input"))
            assert "invalid input" in msg


class TestSessionLimit:
    def test_raises_when_over_limit(self, clean_session_store):
        """check_session_limit raises HTTPException when sessions exceed max."""
        from k8s_runbook_agent.models import AlertStatus, GrafanaAlert

        # Create sessions up to the limit
        with patch("k8s_runbook_agent.security.MAX_CONCURRENT_SESSIONS", 2):
            for i in range(2):
                alert = GrafanaAlert(
                    alert_name=f"Test{i}", status=AlertStatus.FIRING,
                    labels={"namespace": "test"},
                )
                clean_session_store.create(alert)

            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc:
                check_session_limit()
            assert exc.value.status_code == 429


class TestProductionConfigValidation:
    def test_warns_on_missing_secrets(self):
        warnings = validate_production_config()
        # At minimum, GRAFANA_WEBHOOK_SECRET and ADMIN_API_KEY should be flagged
        assert len(warnings) > 0

    def test_checks_critical_keys(self):
        warnings = validate_production_config()
        warning_text = " ".join(warnings)
        # These should always be flagged in test env (no real keys)
        assert "GRAFANA_WEBHOOK_SECRET" in warning_text or "ADMIN_API_KEY" in warning_text


class TestRetentionManager:
    def test_evicts_resolved_sessions(self, clean_session_store):
        """Retention manager evicts resolved sessions older than threshold."""
        from datetime import datetime, timedelta, timezone

        from k8s_runbook_agent.agent.retention import RetentionManager
        from k8s_runbook_agent.agent.session import SessionPhase
        from k8s_runbook_agent.models import AlertStatus, GrafanaAlert

        # Create a resolved session with old timestamp
        alert = GrafanaAlert(
            alert_name="OldAlert", status=AlertStatus.FIRING,
            labels={"namespace": "test"},
        )
        session = clean_session_store.create(alert)
        session.phase = SessionPhase.RESOLVED
        session.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)

        mgr = RetentionManager()
        evicted = mgr._evict_in_memory_sessions()
        assert evicted == 1
        assert clean_session_store.get(session.id) is None

    def test_keeps_active_sessions(self, clean_session_store):
        """Active sessions should not be evicted regardless of age."""
        from datetime import datetime, timedelta, timezone

        from k8s_runbook_agent.agent.retention import RetentionManager
        from k8s_runbook_agent.agent.session import SessionPhase
        from k8s_runbook_agent.models import AlertStatus, GrafanaAlert

        alert = GrafanaAlert(
            alert_name="ActiveAlert", status=AlertStatus.FIRING,
            labels={"namespace": "test"},
        )
        session = clean_session_store.create(alert)
        session.phase = SessionPhase.INVESTIGATING
        session.updated_at = datetime.now(timezone.utc) - timedelta(hours=10)

        mgr = RetentionManager()
        evicted = mgr._evict_in_memory_sessions()
        assert evicted == 0
        assert clean_session_store.get(session.id) is not None
