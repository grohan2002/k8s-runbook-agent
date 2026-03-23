"""Tests for data models and alert parsing."""

import pytest

from k8s_runbook_agent.models import (
    AlertStatus,
    ApprovalState,
    ApprovalStatus,
    Confidence,
    Diagnosis,
    FixProposal,
    GrafanaAlert,
    RiskLevel,
    RunbookMatch,
)


class TestGrafanaAlert:
    def test_parse_basic_alert(self):
        alert = GrafanaAlert(
            alert_name="KubePodCrashLooping",
            status=AlertStatus.FIRING,
            labels={"namespace": "production", "pod": "web-abc123", "severity": "critical"},
        )
        assert alert.namespace == "production"
        assert alert.pod == "web-abc123"
        assert alert.severity == "critical"
        assert alert.status == AlertStatus.FIRING

    def test_defaults_to_default_namespace(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={})
        assert alert.namespace == "default"
        assert alert.severity == "warning"
        assert alert.pod is None

    def test_pod_name_alias(self):
        """Some alert rules use pod_name instead of pod."""
        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            labels={"pod_name": "worker-xyz"},
        )
        assert alert.pod == "worker-xyz"

    def test_summary_from_annotations(self):
        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            annotations={"summary": "Custom summary"},
        )
        assert alert.summary == "Custom summary"

    def test_summary_fallback_to_alert_name(self):
        alert = GrafanaAlert(alert_name="MyAlert", status=AlertStatus.FIRING)
        assert alert.summary == "MyAlert"


class TestDiagnosis:
    def test_create_diagnosis(self):
        diag = Diagnosis(
            root_cause="OOMKilled",
            confidence=Confidence.HIGH,
            evidence=["exit code 137", "memory at limit"],
        )
        assert diag.confidence == Confidence.HIGH
        assert len(diag.evidence) == 2
        assert diag.ruled_out == []

    def test_confidence_values(self):
        assert Confidence.HIGH.value == "high"
        assert Confidence.MEDIUM.value == "medium"
        assert Confidence.LOW.value == "low"


class TestFixProposal:
    def test_create_fix(self):
        fix = FixProposal(
            summary="Increase memory",
            description="Patch deployment",
            risk_level=RiskLevel.LOW,
        )
        assert fix.requires_human_values is False
        assert fix.human_value_fields == []

    def test_fix_with_human_values(self):
        fix = FixProposal(
            summary="Set custom env var",
            description="Need to know the correct value",
            risk_level=RiskLevel.MEDIUM,
            requires_human_values=True,
            human_value_fields=["ENV_VALUE", "REPLICAS"],
        )
        assert fix.requires_human_values is True
        assert len(fix.human_value_fields) == 2

    def test_risk_levels(self):
        assert RiskLevel.LOW.value == "low"
        assert RiskLevel.CRITICAL.value == "critical"


class TestApprovalState:
    def test_default_state(self):
        state = ApprovalState(incident_id="diag-123")
        assert state.status == ApprovalStatus.PENDING
        assert state.approved_by is None
        assert state.executed is False
