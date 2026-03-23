"""Tests for safety guardrails."""

import pytest

from k8s_runbook_agent.agent.guardrails import evaluate_guardrails, BLOCKED_NAMESPACES
from k8s_runbook_agent.agent.session import DiagnosisSession
from k8s_runbook_agent.models import (
    AlertStatus,
    Confidence,
    GrafanaAlert,
    RiskLevel,
)


def _make_session(
    namespace: str = "production",
    confidence: Confidence = Confidence.HIGH,
    risk: RiskLevel = RiskLevel.LOW,
    description: str = "patch deployment",
    rollback: str = "kubectl rollout undo",
    dry_run: str = "ok",
    requires_human: bool = False,
    human_fields: list[str] | None = None,
) -> DiagnosisSession:
    alert = GrafanaAlert(
        alert_name="Test",
        status=AlertStatus.FIRING,
        labels={"namespace": namespace},
    )
    session = DiagnosisSession(alert)
    session.set_diagnosis("test cause", confidence, ["evidence"])
    session.set_fix_proposal(
        summary="test fix",
        description=description,
        risk_level=risk,
        dry_run_output=dry_run,
        rollback_plan=rollback,
        requires_human_values=requires_human,
        human_value_fields=human_fields or [],
    )
    return session


class TestNamespaceBlocklist:
    @pytest.mark.parametrize("ns", sorted(BLOCKED_NAMESPACES))
    def test_blocks_protected_namespaces(self, ns):
        session = _make_session(namespace=ns)
        result = evaluate_guardrails(session)
        assert not result.passed
        assert any("blocklist" in r for r in result.blocked_reasons)

    def test_allows_user_namespaces(self):
        session = _make_session(namespace="my-app")
        result = evaluate_guardrails(session)
        assert result.passed


class TestRiskLevel:
    def test_blocks_critical_risk(self):
        session = _make_session(risk=RiskLevel.CRITICAL)
        result = evaluate_guardrails(session)
        assert not result.passed
        assert any("CRITICAL" in r for r in result.blocked_reasons)

    @pytest.mark.parametrize("risk", [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH])
    def test_allows_non_critical(self, risk):
        session = _make_session(risk=risk)
        result = evaluate_guardrails(session)
        assert result.passed


class TestConfidence:
    def test_blocks_low_confidence(self):
        session = _make_session(confidence=Confidence.LOW)
        result = evaluate_guardrails(session)
        assert not result.passed

    def test_warns_medium_confidence(self):
        session = _make_session(confidence=Confidence.MEDIUM)
        result = evaluate_guardrails(session)
        assert result.passed  # Not blocked, just warned
        assert any("MEDIUM" in w for w in result.warnings)

    def test_passes_high_confidence(self):
        session = _make_session(confidence=Confidence.HIGH)
        result = evaluate_guardrails(session)
        assert result.passed
        assert len(result.warnings) == 0


class TestImageSafety:
    def test_blocks_latest_tag(self):
        session = _make_session(description="image: myapp:latest")
        result = evaluate_guardrails(session)
        assert not result.passed
        assert any("latest" in r for r in result.blocked_reasons)

    def test_allows_versioned_tag(self):
        session = _make_session(description="image: myapp:v1.2.3")
        result = evaluate_guardrails(session)
        assert result.passed


class TestReplicaGuardrails:
    def test_warns_zero_replicas(self):
        session = _make_session(description="replicas: 0")
        result = evaluate_guardrails(session)
        assert result.passed  # Warning, not block
        assert any("0" in w for w in result.warnings)

    def test_blocks_excessive_replicas(self):
        session = _make_session(description="replicas: 999")
        result = evaluate_guardrails(session)
        assert not result.passed


class TestHumanValues:
    def test_blocks_when_human_values_needed(self):
        session = _make_session(requires_human=True, human_fields=["MEMORY_LIMIT"])
        result = evaluate_guardrails(session)
        assert not result.passed
        assert any("human" in r.lower() for r in result.blocked_reasons)


class TestMissingRollback:
    def test_warns_no_rollback_plan(self):
        session = _make_session(rollback="")
        result = evaluate_guardrails(session)
        assert result.passed  # Warning, not block
        assert any("rollback" in w.lower() for w in result.warnings)


class TestNoFixProposal:
    def test_blocks_when_no_fix(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={"namespace": "prod"},
        )
        session = DiagnosisSession(alert)
        result = evaluate_guardrails(session)
        assert not result.passed
        assert any("No fix" in r for r in result.blocked_reasons)
