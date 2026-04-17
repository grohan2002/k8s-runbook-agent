"""Tests for SLO impact awareness feature."""

import pytest

from k8s_runbook_agent.agent.guardrails import evaluate_guardrails
from k8s_runbook_agent.agent.session import DiagnosisSession
from k8s_runbook_agent.models import (
    AlertStatus,
    Confidence,
    GrafanaAlert,
    RiskLevel,
)


class TestGrafanaAlertSLOProperties:
    def test_slo_name_from_annotations(self):
        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            annotations={"slo_name": "api-availability"},
        )
        assert alert.slo_name == "api-availability"

    def test_slo_name_missing(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, annotations={},
        )
        assert alert.slo_name is None

    def test_slo_name_empty_string_returns_none(self):
        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            annotations={"slo_name": ""},
        )
        assert alert.slo_name is None

    def test_error_budget_remaining_valid(self):
        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            annotations={"error_budget_remaining": "12.5"},
        )
        assert alert.error_budget_remaining == 12.5

    def test_error_budget_remaining_missing(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, annotations={},
        )
        assert alert.error_budget_remaining is None

    def test_error_budget_remaining_invalid(self):
        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            annotations={"error_budget_remaining": "not-a-number"},
        )
        assert alert.error_budget_remaining is None

    def test_error_budget_remaining_zero(self):
        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            annotations={"error_budget_remaining": "0"},
        )
        assert alert.error_budget_remaining == 0.0


class TestSLOGuardrail:
    def _make_session(self, error_budget: float | None, slo_name: str = "api-slo") -> DiagnosisSession:
        annotations = {}
        if slo_name:
            annotations["slo_name"] = slo_name
        if error_budget is not None:
            annotations["error_budget_remaining"] = str(error_budget)

        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            labels={"namespace": "production"},
            annotations=annotations,
        )
        session = DiagnosisSession(alert)
        session.set_diagnosis("test", Confidence.HIGH, ["evidence"])
        session.set_fix_proposal(
            summary="fix", description="desc", risk_level=RiskLevel.LOW,
            dry_run_output="ok", rollback_plan="rollback",
        )
        return session

    def test_warns_when_budget_below_10_percent(self):
        session = self._make_session(error_budget=5.0)
        result = evaluate_guardrails(session)
        assert result.passed  # warning, not block
        assert any("error budget" in w.lower() for w in result.warnings)
        assert any("5.0%" in w for w in result.warnings)

    def test_no_warning_when_budget_above_10_percent(self):
        session = self._make_session(error_budget=50.0)
        result = evaluate_guardrails(session)
        assert result.passed
        assert not any("error budget" in w.lower() for w in result.warnings)

    def test_no_warning_when_no_slo_annotation(self):
        session = self._make_session(error_budget=None, slo_name="")
        result = evaluate_guardrails(session)
        assert result.passed
        assert not any("error budget" in w.lower() for w in result.warnings)


class TestPromptInjection:
    def test_slo_in_alert_context(self):
        from k8s_runbook_agent.agent.prompts import format_alert_context

        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            annotations={"slo_name": "api-availability", "error_budget_remaining": "8.5"},
        )
        context = format_alert_context(alert)
        assert "api-availability" in context
        assert "8.5%" in context
        assert "critically low" in context.lower()

    def test_high_budget_no_warning_in_prompt(self):
        from k8s_runbook_agent.agent.prompts import format_alert_context

        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            annotations={"slo_name": "api-availability", "error_budget_remaining": "45.0"},
        )
        context = format_alert_context(alert)
        assert "45.0%" in context
        assert "critically low" not in context.lower()

    def test_no_slo_means_no_slo_section(self):
        from k8s_runbook_agent.agent.prompts import format_alert_context

        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, annotations={},
        )
        context = format_alert_context(alert)
        assert "SLO:" not in context
