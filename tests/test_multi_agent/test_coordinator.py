"""Tests for the Coordinator Agent."""

import pytest

from k8s_runbook_agent.agent.multi_agent.coordinator import CoordinatorAgent
from k8s_runbook_agent.agent.multi_agent.prompts.coordinator_prompt import format_specialist_findings
from k8s_runbook_agent.agent.session import DiagnosisSession
from k8s_runbook_agent.models import AlertStatus, Confidence, GrafanaAlert, RiskLevel


@pytest.fixture
def pod_session():
    alert = GrafanaAlert(
        alert_name="KubePodCrashLooping", status=AlertStatus.FIRING,
        labels={"namespace": "prod", "pod": "api-abc", "severity": "critical"},
    )
    session = DiagnosisSession(alert)
    session.specialist_domain = "pod"
    session.set_diagnosis(
        root_cause="OOMKilled — memory limit 256Mi too low",
        confidence=Confidence.HIGH,
        evidence=["exit code 137", "memory 254Mi/256Mi"],
    )
    session.set_fix_proposal(
        summary="Increase memory to 512Mi",
        description="Patch deployment",
        risk_level=RiskLevel.LOW,
    )
    return session


@pytest.fixture
def network_session():
    alert = GrafanaAlert(
        alert_name="KubeServiceWithNoEndpoints", status=AlertStatus.FIRING,
        labels={"namespace": "prod", "service": "api-svc", "severity": "warning"},
    )
    session = DiagnosisSession(alert)
    session.specialist_domain = "network"
    session.set_diagnosis(
        root_cause="Service api-svc has 0 ready endpoints due to pod crashes",
        confidence=Confidence.MEDIUM,
        evidence=["0/3 endpoints ready", "pods restarting"],
    )
    return session


class TestFormatSpecialistFindings:
    def test_formats_single_session(self, pod_session):
        text = format_specialist_findings([pod_session])
        assert "POD Domain" in text
        assert "OOMKilled" in text
        assert "512Mi" in text

    def test_formats_multiple_sessions(self, pod_session, network_session):
        text = format_specialist_findings([pod_session, network_session])
        assert "Specialist 1" in text
        assert "Specialist 2" in text
        assert "POD" in text
        assert "NETWORK" in text

    def test_includes_evidence(self, pod_session):
        text = format_specialist_findings([pod_session])
        assert "exit code 137" in text

    def test_includes_fix_proposal(self, pod_session):
        text = format_specialist_findings([pod_session])
        assert "Increase memory" in text


class TestCoordinatorAgent:
    def test_initialization(self):
        coord = CoordinatorAgent()
        assert coord.model  # Should have a model set

    @pytest.mark.asyncio
    async def test_requires_sessions(self):
        coord = CoordinatorAgent()
        with pytest.raises(ValueError, match="No sessions"):
            await coord.synthesize([], "test-key")
