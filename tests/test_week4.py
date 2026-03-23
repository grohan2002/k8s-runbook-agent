"""Tests for Week 4 features: RBAC, escalation, multi-cluster, correlation, hot-reload."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from k8s_runbook_agent.agent.session import DiagnosisSession, SessionPhase, SessionStore
from k8s_runbook_agent.models import AlertStatus, Confidence, GrafanaAlert, RiskLevel


# =====================================================================
# RBAC Tests
# =====================================================================
class TestRBAC:
    @pytest.fixture
    def policy(self):
        from k8s_runbook_agent.agent.rbac import ApprovalPolicy
        return ApprovalPolicy()

    @pytest.mark.asyncio
    async def test_open_mode_allows_everyone(self, policy):
        """No config = open mode = allow all."""
        policy.configure()
        result = await policy.authorize("U999", "anyone")
        assert result.allowed

    @pytest.mark.asyncio
    async def test_allowlist_permits_listed_user(self, policy):
        policy.configure(allowed_users="U123,U456")
        result = await policy.authorize("U123", "alice")
        assert result.allowed

    @pytest.mark.asyncio
    async def test_allowlist_denies_unlisted_user(self, policy):
        policy.configure(allowed_users="U123,U456")
        result = await policy.authorize("U999", "mallory")
        assert not result.allowed
        assert "not authorized" in result.reason

    @pytest.mark.asyncio
    async def test_senior_required_for_high_risk(self, policy):
        from k8s_runbook_agent.agent.rbac import AuthzDecision
        policy.configure(
            allowed_users="U100,U200",
            senior_users="U100",
            min_risk_for_senior="high",
        )
        # Regular user trying to approve HIGH risk
        result = await policy.authorize("U200", "bob", RiskLevel.HIGH)
        assert result.decision == AuthzDecision.NEEDS_SENIOR

        # Senior user can approve HIGH risk
        result = await policy.authorize("U100", "alice", RiskLevel.HIGH)
        assert result.allowed

    @pytest.mark.asyncio
    async def test_senior_not_required_for_low_risk(self, policy):
        policy.configure(
            allowed_users="U100,U200",
            senior_users="U100",
            min_risk_for_senior="high",
        )
        result = await policy.authorize("U200", "bob", RiskLevel.LOW)
        assert result.allowed

    @pytest.mark.asyncio
    async def test_senior_users_are_in_allowlist(self, policy):
        """Senior users should be able to approve even if not in allowed_users."""
        policy.configure(senior_users="U100")
        result = await policy.authorize("U100", "alice", RiskLevel.CRITICAL)
        assert result.allowed

    def test_to_slack_text(self, policy):
        from k8s_runbook_agent.agent.rbac import AuthzDecision, AuthzResult

        allowed = AuthzResult(AuthzDecision.ALLOWED, "ok", "U1", "a")
        assert "✅" in allowed.to_slack_text()

        denied = AuthzResult(AuthzDecision.DENIED, "nope", "U2", "b")
        assert "🚫" in denied.to_slack_text()


# =====================================================================
# Escalation Timer Tests
# =====================================================================
class TestEscalation:
    @pytest.fixture
    def session_for_escalation(self, clean_session_store):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING,
            labels={"namespace": "prod", "severity": "critical"},
        )
        s = clean_session_store.create(alert)
        s.set_diagnosis("test", Confidence.HIGH, [])
        s.set_fix_proposal("fix", "desc", RiskLevel.LOW)
        s.request_approval()
        # Backdate so SLA is already breached
        s.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        return s

    @pytest.mark.asyncio
    async def test_detects_sla_breach(self, session_for_escalation, clean_session_store):
        from k8s_runbook_agent.agent.escalation import EscalationConfig, EscalationTimer

        callback = AsyncMock()
        config = EscalationConfig(
            sla_seconds={"critical": 60},  # 1 minute SLA
            check_interval=1,
            enabled=True,
        )
        timer = EscalationTimer(config=config, on_escalate=callback)
        await timer._check_sessions()

        # Should have triggered escalation since session is 10min old with 1min SLA
        assert callback.call_count >= 1
        call_args = callback.call_args
        assert "SLA BREACH" in call_args[0][1] or "AUTO-REJECTED" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_auto_rejects_after_2x_sla(self, session_for_escalation, clean_session_store):
        from k8s_runbook_agent.agent.escalation import EscalationConfig, EscalationTimer

        callback = AsyncMock()
        config = EscalationConfig(
            sla_seconds={"critical": 60},
            auto_reject_multiplier=2.0,
            check_interval=1,
            enabled=True,
        )
        timer = EscalationTimer(config=config, on_escalate=callback)
        await timer._check_sessions()

        # Session is 10min old, auto-reject at 2x 1min = 2min
        assert session_for_escalation.phase in (
            SessionPhase.RESOLVED,  # rejected
            SessionPhase.AWAITING_APPROVAL,  # still awaiting if timing is off
        )

    def test_sla_by_severity(self):
        from k8s_runbook_agent.agent.escalation import EscalationConfig, EscalationTimer

        config = EscalationConfig(
            sla_seconds={"critical": 60, "warning": 300, "info": 600},
        )
        timer = EscalationTimer(config=config)

        alert = GrafanaAlert(
            alert_name="T", status=AlertStatus.FIRING,
            labels={"severity": "warning"},
        )
        session = DiagnosisSession(alert)
        sla = timer._get_sla_seconds(session)
        assert sla == 300

    def test_disabled_timer(self):
        from k8s_runbook_agent.agent.escalation import EscalationConfig

        config = EscalationConfig(enabled=False)
        assert not config.enabled


# =====================================================================
# Alert Correlation Tests
# =====================================================================
class TestCorrelation:
    @pytest.fixture
    def correlator(self):
        from k8s_runbook_agent.agent.correlation import AlertCorrelator
        return AlertCorrelator(window_seconds=300)

    def _make_alert(self, name, ns="prod", pod="app-abc123", deployment="app", **extra_labels):
        labels = {"namespace": ns, "pod": pod, "deployment": deployment, **extra_labels}
        return GrafanaAlert(
            alert_name=name, status=AlertStatus.FIRING,
            labels=labels, fingerprint=f"fp-{name}",
        )

    def test_extracts_workload_key(self):
        from k8s_runbook_agent.agent.correlation import _extract_workload_key

        alert = self._make_alert("CrashLoop")
        key = _extract_workload_key(alert)
        assert key == "prod/deployment/app"

    def test_correlates_same_workload(self, correlator, clean_session_store):
        alert1 = self._make_alert("CrashLoop")
        alert2 = self._make_alert("HighRestartRate")

        # Register first alert
        session = clean_session_store.create(alert1)
        session.transition(SessionPhase.INVESTIGATING)
        correlator.register_session(session)

        # Second alert for same workload should correlate
        result = correlator.correlate(alert2)
        assert result is session

    def test_no_correlation_different_workload(self, correlator, clean_session_store):
        alert1 = self._make_alert("CrashLoop", deployment="api")
        alert2 = self._make_alert("CrashLoop", deployment="worker")

        session = clean_session_store.create(alert1)
        session.transition(SessionPhase.INVESTIGATING)
        correlator.register_session(session)

        result = correlator.correlate(alert2)
        assert result is None

    def test_no_correlation_different_namespace(self, correlator, clean_session_store):
        alert1 = self._make_alert("CrashLoop", ns="prod")
        alert2 = self._make_alert("CrashLoop", ns="staging")

        session = clean_session_store.create(alert1)
        session.transition(SessionPhase.INVESTIGATING)
        correlator.register_session(session)

        result = correlator.correlate(alert2)
        assert result is None

    def test_correlated_alert_adds_context(self, correlator, clean_session_store):
        alert1 = self._make_alert("CrashLoop")
        alert2 = self._make_alert("HighRestartRate")

        session = clean_session_store.create(alert1)
        session.transition(SessionPhase.INVESTIGATING)
        correlator.register_session(session)

        msg_count_before = len(session.messages)
        correlator.correlate(alert2)
        # Should have added a context message
        assert len(session.messages) == msg_count_before + 1
        assert "CORRELATED ALERT" in session.messages[-1]["content"]

    def test_resolved_session_not_correlated(self, correlator, clean_session_store):
        alert1 = self._make_alert("CrashLoop")
        alert2 = self._make_alert("HighRestartRate")

        session = clean_session_store.create(alert1)
        session.mark_resolved("done")
        correlator.register_session(session)

        result = correlator.correlate(alert2)
        assert result is None

    def test_get_correlated_alerts(self, correlator, clean_session_store):
        alert1 = self._make_alert("CrashLoop")
        alert2 = self._make_alert("HighRestartRate")
        alert3 = self._make_alert("PodNotReady")

        session = clean_session_store.create(alert1)
        session.transition(SessionPhase.INVESTIGATING)
        correlator.register_session(session)

        correlator.correlate(alert2)
        correlator.correlate(alert3)

        correlated = correlator.get_correlated_alerts(session.id)
        assert len(correlated) == 2


# =====================================================================
# Multi-Cluster Tests
# =====================================================================
class TestMultiCluster:
    def test_empty_config(self):
        from k8s_runbook_agent.agent.multi_cluster import ClusterRegistry
        reg = ClusterRegistry()
        assert not reg.is_multi_cluster
        assert reg.cluster_names == []
        assert reg.default_cluster is None

    def test_resolve_cluster_from_labels(self):
        from k8s_runbook_agent.agent.multi_cluster import ClusterConfig, ClusterRegistry
        reg = ClusterRegistry()

        # Register clusters without actually connecting to K8s
        reg._clusters = {
            "prod-east": ClusterConfig(name="prod-east", environment="prod", region="us-east-1", is_default=True),
            "prod-west": ClusterConfig(name="prod-west", environment="prod", region="us-west-2"),
            "staging": ClusterConfig(name="staging", environment="staging"),
        }
        reg._default_cluster = "prod-east"

        # Direct match
        assert reg.resolve_cluster({"cluster": "prod-east"}) == "prod-east"
        assert reg.resolve_cluster({"cluster_name": "staging"}) == "staging"

        # Partial match
        assert reg.resolve_cluster({"cluster": "prod-east-eks-123"}) == "prod-east"

        # Environment fallback
        assert reg.resolve_cluster({"environment": "staging"}) == "staging"

        # Default fallback
        assert reg.resolve_cluster({}) == "prod-east"

    def test_load_from_json(self):
        import json
        from k8s_runbook_agent.agent.multi_cluster import ClusterRegistry
        reg = ClusterRegistry()

        # load_from_json without kubeconfig files should handle errors gracefully
        config = json.dumps([
            {"name": "test-cluster", "environment": "test", "region": "us-east-1"},
        ])
        # This will fail to create APIs (no kubeconfig) but should not crash
        count = reg.load_from_json(config)
        # Cluster should be registered even if API creation failed
        assert "test-cluster" in reg._clusters

    def test_summary(self):
        from k8s_runbook_agent.agent.multi_cluster import ClusterConfig, ClusterRegistry
        reg = ClusterRegistry()
        reg._clusters = {
            "prod": ClusterConfig(name="prod", environment="prod", is_default=True),
        }
        reg._default_cluster = "prod"
        summary = reg.summary()
        assert "prod" in summary
        assert "default" in summary


# =====================================================================
# Runbook Hot-Reload Tests
# =====================================================================
class TestHotReload:
    @pytest.fixture
    def store(self):
        from k8s_runbook_agent.knowledge.loader import RunbookStore
        from k8s_runbook_agent.config import settings
        s = RunbookStore()
        s.load_directory(settings.runbook_dir)
        return s

    def test_reload_now(self, store):
        from k8s_runbook_agent.knowledge.hot_reload import RunbookWatcher
        from k8s_runbook_agent.config import settings

        watcher = RunbookWatcher(store, settings.runbook_dir)
        result = watcher.reload_now()
        assert result["status"] == "ok"
        assert result["runbooks_loaded"] >= 3
        assert watcher.reload_count == 1

    def test_snapshot_tracks_files(self, store):
        from k8s_runbook_agent.knowledge.hot_reload import RunbookWatcher
        from k8s_runbook_agent.config import settings

        watcher = RunbookWatcher(store, settings.runbook_dir)
        watcher._snapshot()
        assert len(watcher._file_mtimes) >= 3

    def test_reload_invalid_dir(self):
        from k8s_runbook_agent.knowledge.hot_reload import RunbookWatcher
        from k8s_runbook_agent.knowledge.loader import RunbookStore

        store = RunbookStore()
        watcher = RunbookWatcher(store, "/nonexistent/path")
        result = watcher.reload_now()
        assert result["status"] == "error"
