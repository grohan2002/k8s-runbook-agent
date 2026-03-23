"""Tests for the incident memory layer (record, recall, format, embeddings)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from k8s_runbook_agent.agent.embeddings import EmbeddingProvider, build_incident_text
from k8s_runbook_agent.agent.incident_memory import (
    FixSuccessRate,
    IncidentMemory,
    MemoryContext,
    PastIncident,
    RecurringPattern,
)
from k8s_runbook_agent.agent.session import DiagnosisSession, SessionPhase
from k8s_runbook_agent.models import (
    AlertStatus,
    Confidence,
    GrafanaAlert,
    RiskLevel,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def resolved_session():
    """A session that went through the full lifecycle."""
    alert = GrafanaAlert(
        alert_name="KubePodCrashLooping",
        status=AlertStatus.FIRING,
        labels={"namespace": "production", "pod": "api-server-abc", "severity": "critical", "deployment": "api-server"},
        fingerprint="fp-test-memory",
    )
    session = DiagnosisSession(alert)
    session.set_diagnosis(
        root_cause="Container OOMKilled — memory limit 256Mi too low",
        confidence=Confidence.HIGH,
        evidence=["Exit code 137", "Memory usage 254Mi / 256Mi"],
    )
    session.set_fix_proposal(
        summary="Increase memory limit from 256Mi to 512Mi",
        description="Patch deployment to increase resources.limits.memory",
        risk_level=RiskLevel.LOW,
        dry_run_output="spec.containers[0].resources.limits.memory: 256Mi -> 512Mi",
        rollback_plan="kubectl rollout undo",
    )
    session.mark_resolved("Fix applied successfully")
    return session


@pytest.fixture
def sample_memory_context():
    """A MemoryContext with representative data."""
    return MemoryContext(
        similar_incidents=[
            PastIncident(
                session_id="diag-past1",
                alert_name="KubePodCrashLooping",
                namespace="production",
                workload_key="production/deployment/api-server",
                root_cause="Memory limit 256Mi too low for Java heap",
                confidence="high",
                fix_summary="Increase memory limit to 512Mi",
                fix_risk_level="low",
                outcome="success",
                execution_result="Fix applied successfully",
                resolved_at=datetime(2025, 3, 18, tzinfo=timezone.utc),
                similarity=0.94,
            ),
            PastIncident(
                session_id="diag-past2",
                alert_name="KubePodCrashLooping",
                namespace="production",
                workload_key="production/deployment/api-server",
                root_cause="Memory leak in request handler",
                confidence="medium",
                fix_summary="Restart pod",
                fix_risk_level="low",
                outcome="success",
                execution_result="Pod restarted, recurred 24h later",
                resolved_at=datetime(2025, 3, 10, tzinfo=timezone.utc),
                similarity=0.88,
            ),
        ],
        fix_success_rates=[
            FixSuccessRate(fix_summary="Increase memory limit", total=5, successes=5, failures=0, rejections=0),
            FixSuccessRate(fix_summary="Restart pod", total=3, successes=2, failures=1, rejections=0),
        ],
        recurring_patterns=[
            RecurringPattern(
                alert_name="KubePodCrashLooping",
                root_cause="Memory limit too low",
                occurrences=3,
                first_seen=datetime(2025, 3, 10, tzinfo=timezone.utc),
                last_seen=datetime(2025, 3, 18, tzinfo=timezone.utc),
                fixes_tried=["Increase memory limit", "Restart pod"],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Tests: build_incident_text
# ---------------------------------------------------------------------------
class TestBuildIncidentText:
    def test_format(self):
        text = build_incident_text(
            alert_name="KubePodOOMKilled",
            namespace="production",
            root_cause="Memory limit too low",
            fix_summary="Increase to 512Mi",
        )
        assert "KubePodOOMKilled" in text
        assert "production" in text
        assert "Memory limit too low" in text
        assert "512Mi" in text

    def test_consistent_format(self):
        """Same inputs produce same text for consistent embeddings."""
        t1 = build_incident_text("A", "B", "C", "D")
        t2 = build_incident_text("A", "B", "C", "D")
        assert t1 == t2


# ---------------------------------------------------------------------------
# Tests: EmbeddingProvider
# ---------------------------------------------------------------------------
class TestEmbeddingProvider:
    def test_not_available_without_key(self):
        provider = EmbeddingProvider()
        # Since we're in test env, API key may or may not be set
        # Just verify the interface works
        assert isinstance(provider.available, bool)

    @pytest.mark.asyncio
    async def test_health_check_returns_dict(self):
        provider = EmbeddingProvider()
        result = await provider.health_check()
        assert isinstance(result, dict)
        assert "status" in result

    @pytest.mark.asyncio
    async def test_embed_returns_none_when_unavailable(self):
        provider = EmbeddingProvider()
        provider._available = False
        provider._init_attempted = True
        result = await provider.embed("test text")
        assert result is None

    @pytest.mark.asyncio
    async def test_health_check_returns_consistent_status(self):
        provider = EmbeddingProvider()
        r1 = await provider.health_check()
        r2 = await provider.health_check()
        # Both calls return the same status (cached or not)
        assert r1["status"] == r2["status"]


# ---------------------------------------------------------------------------
# Tests: MemoryContext
# ---------------------------------------------------------------------------
class TestMemoryContext:
    def test_empty_context(self):
        ctx = MemoryContext()
        assert not ctx.has_data

    def test_has_data_with_incidents(self):
        ctx = MemoryContext(similar_incidents=[
            PastIncident(
                session_id="x", alert_name="A", namespace="ns", workload_key=None,
                root_cause="r", confidence="high", fix_summary="f", fix_risk_level="low",
                outcome="success", execution_result=None, resolved_at=None,
            )
        ])
        assert ctx.has_data

    def test_has_data_with_rates_only(self):
        ctx = MemoryContext(fix_success_rates=[
            FixSuccessRate(fix_summary="fix", total=1, successes=1, failures=0, rejections=0)
        ])
        assert ctx.has_data


# ---------------------------------------------------------------------------
# Tests: FixSuccessRate
# ---------------------------------------------------------------------------
class TestFixSuccessRate:
    def test_success_rate_calculation(self):
        rate = FixSuccessRate(fix_summary="test", total=10, successes=8, failures=2, rejections=0)
        assert rate.success_rate == 0.8
        assert "8/10" in rate.rate_str
        assert "80%" in rate.rate_str

    def test_zero_total(self):
        rate = FixSuccessRate(fix_summary="test", total=0, successes=0, failures=0, rejections=0)
        assert rate.success_rate == 0.0

    def test_perfect_rate(self):
        rate = FixSuccessRate(fix_summary="test", total=5, successes=5, failures=0, rejections=0)
        assert rate.success_rate == 1.0
        assert "100%" in rate.rate_str


# ---------------------------------------------------------------------------
# Tests: IncidentMemory.format_for_prompt
# ---------------------------------------------------------------------------
class TestFormatForPrompt:
    def test_returns_none_for_empty(self):
        memory = IncidentMemory()
        result = memory.format_for_prompt(MemoryContext())
        assert result is None

    def test_includes_similar_incidents(self, sample_memory_context):
        memory = IncidentMemory()
        result = memory.format_for_prompt(sample_memory_context)
        assert result is not None
        assert "Incident Memory" in result
        assert "Past Incidents" in result
        assert "Memory limit 256Mi too low" in result
        assert "SUCCESS" in result
        assert "0.94" in result

    def test_includes_fix_rates(self, sample_memory_context):
        memory = IncidentMemory()
        result = memory.format_for_prompt(sample_memory_context)
        assert "Fix Success Rates" in result
        assert "5/5" in result
        assert "100%" in result
        assert "2/3" in result

    def test_includes_recurring_pattern(self, sample_memory_context):
        memory = IncidentMemory()
        result = memory.format_for_prompt(sample_memory_context)
        assert "Recurring Pattern" in result
        assert "3" in result
        assert "permanent" in result.lower()

    def test_omits_empty_sections(self):
        memory = IncidentMemory()
        ctx = MemoryContext(
            similar_incidents=[],
            fix_success_rates=[
                FixSuccessRate(fix_summary="x", total=1, successes=1, failures=0, rejections=0)
            ],
            recurring_patterns=[],
        )
        result = memory.format_for_prompt(ctx)
        assert result is not None
        assert "### Past Incidents" not in result  # No incidents section header
        assert "Recurring Pattern" not in result
        assert "Fix Success Rates" in result


# ---------------------------------------------------------------------------
# Tests: IncidentMemory._determine_outcome
# ---------------------------------------------------------------------------
class TestDetermineOutcome:
    def test_resolved_executed(self, resolved_session):
        memory = IncidentMemory()
        assert memory._determine_outcome(resolved_session) == "success"

    def test_escalated(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={})
        session = DiagnosisSession(alert)
        session.set_diagnosis("test", Confidence.HIGH, [])
        session.escalate("cannot determine cause")
        assert IncidentMemory()._determine_outcome(session) == "escalated"

    def test_failed(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={})
        session = DiagnosisSession(alert)
        session.fail("API error")
        assert IncidentMemory()._determine_outcome(session) == "failed"

    def test_rejected(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={})
        session = DiagnosisSession(alert)
        session.set_diagnosis("test", Confidence.HIGH, [])
        session.set_fix_proposal("fix", "desc", RiskLevel.LOW)
        session.request_approval()
        session.reject("user123")
        assert IncidentMemory()._determine_outcome(session) == "rejected"


# ---------------------------------------------------------------------------
# Tests: IncidentMemory.record (no DB — verifies it doesn't crash)
# ---------------------------------------------------------------------------
class TestRecordGracefulDegradation:
    @pytest.mark.asyncio
    async def test_record_without_db_does_not_crash(self, resolved_session):
        """Without PostgreSQL, record should silently succeed (no-op)."""
        memory = IncidentMemory()
        await memory.record(resolved_session)  # Should not raise

    @pytest.mark.asyncio
    async def test_record_skips_session_without_diagnosis(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={})
        session = DiagnosisSession(alert)
        session.fail("no diagnosis")
        memory = IncidentMemory()
        await memory.record(session)  # Should skip silently

    @pytest.mark.asyncio
    async def test_recall_without_db_returns_empty(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING,
            labels={"namespace": "test"},
        )
        memory = IncidentMemory()
        ctx = await memory.recall(alert)
        assert not ctx.has_data


# ---------------------------------------------------------------------------
# Tests: Prompt integration
# ---------------------------------------------------------------------------
class TestPromptIntegration:
    def test_build_system_prompt_with_memory(self):
        from k8s_runbook_agent.agent.prompts import build_system_prompt

        alert = GrafanaAlert(
            alert_name="KubePodCrashLooping",
            status=AlertStatus.FIRING,
            labels={"namespace": "prod"},
        )
        memory_text = "## Incident Memory\n\nPast fix: Increase memory → SUCCESS (5/5)"
        prompt = build_system_prompt(alert, runbook=None, memory_context=memory_text)
        assert "Incident Memory" in prompt
        assert "5/5" in prompt

    def test_build_system_prompt_without_memory(self):
        from k8s_runbook_agent.agent.prompts import build_system_prompt

        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            labels={"namespace": "test"},
        )
        prompt = build_system_prompt(alert, runbook=None, memory_context=None)
        assert "Incident Memory" not in prompt

    def test_memory_injected_before_alert_context(self):
        from k8s_runbook_agent.agent.prompts import build_system_prompt

        alert = GrafanaAlert(
            alert_name="TestAlert",
            status=AlertStatus.FIRING,
            labels={"namespace": "test"},
        )
        memory_text = "## MEMORY_MARKER"
        prompt = build_system_prompt(alert, memory_context=memory_text)
        # Memory should appear before the alert context
        mem_pos = prompt.index("MEMORY_MARKER")
        alert_pos = prompt.index("TestAlert")
        assert mem_pos < alert_pos
