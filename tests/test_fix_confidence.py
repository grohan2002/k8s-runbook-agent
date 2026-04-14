"""Tests for fix confidence scoring."""

import pytest

from k8s_runbook_agent.agent.guardrails import evaluate_guardrails
from k8s_runbook_agent.agent.incident_memory import FixConfidence
from k8s_runbook_agent.agent.session import DiagnosisSession
from k8s_runbook_agent.models import (
    AlertStatus,
    Confidence,
    GrafanaAlert,
    RiskLevel,
)


class TestFixConfidence:
    def test_high_confidence_with_history(self):
        """HIGH diagnosis + 100% success rate + full evidence → ~0.88."""
        fc = FixConfidence(
            score=0.88,
            diagnosis_weight=0.4,    # HIGH=1.0 * 0.4
            history_weight=0.4,      # 1.0 success rate * 0.4
            evidence_weight=0.08,    # 2/5 * 0.2
            fix_success_rate=1.0,
            fix_success_count=3,
            fix_total_count=3,
            has_history=True,
        )
        assert fc.percentage == 88
        assert fc.has_history is True

    def test_no_history_defaults_neutral(self):
        """No history defaults to 0.5 rate → score ~0.60."""
        fc = FixConfidence(
            score=0.60,
            diagnosis_weight=0.4,
            history_weight=0.2,     # 0.5 default * 0.4
            evidence_weight=0.0,
            fix_success_rate=0.5,
            fix_success_count=0,
            fix_total_count=0,
            has_history=False,
        )
        assert fc.has_history is False

    def test_low_everything(self):
        """LOW diagnosis + 0% success + 1 evidence → score ~0.16."""
        fc = FixConfidence(
            score=0.16,
            diagnosis_weight=0.12,   # LOW=0.3 * 0.4
            history_weight=0.0,      # 0/3 * 0.4
            evidence_weight=0.04,    # 1/5 * 0.2
            fix_success_rate=0.0,
            fix_success_count=0,
            fix_total_count=3,
            has_history=True,
        )
        assert fc.percentage == 16

    def test_display_str_with_history(self):
        fc = FixConfidence(
            score=0.87, diagnosis_weight=0.4, history_weight=0.37,
            evidence_weight=0.1, fix_success_rate=0.92,
            fix_success_count=5, fix_total_count=5, has_history=True,
        )
        display = fc.display_str("high")
        assert "87%" in display
        assert "5/5" in display
        assert "HIGH" in display

    def test_display_str_without_history(self):
        fc = FixConfidence(
            score=0.60, diagnosis_weight=0.4, history_weight=0.2,
            evidence_weight=0.0, fix_success_rate=0.5,
            fix_success_count=0, fix_total_count=0, has_history=False,
        )
        display = fc.display_str("medium")
        assert "60%" in display
        assert "past successes" not in display
        assert "MEDIUM" in display

    def test_percentage_rounds(self):
        fc = FixConfidence(
            score=0.333, diagnosis_weight=0, history_weight=0,
            evidence_weight=0, fix_success_rate=0,
            fix_success_count=0, fix_total_count=0, has_history=False,
        )
        assert fc.percentage == 33

    def test_perfect_score(self):
        fc = FixConfidence(
            score=1.0, diagnosis_weight=0.4, history_weight=0.4,
            evidence_weight=0.2, fix_success_rate=1.0,
            fix_success_count=10, fix_total_count=10, has_history=True,
        )
        assert fc.percentage == 100


class TestFixConfidenceGuardrail:
    def _make_session(self, score: float) -> DiagnosisSession:
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING,
            labels={"namespace": "production"},
        )
        session = DiagnosisSession(alert)
        session.set_diagnosis("test cause", Confidence.HIGH, ["evidence"])
        session.set_fix_proposal(
            summary="test fix", description="desc", risk_level=RiskLevel.LOW,
            dry_run_output="ok", rollback_plan="rollback",
        )
        session.fix_confidence = FixConfidence(
            score=score, diagnosis_weight=0, history_weight=0,
            evidence_weight=0, fix_success_rate=0,
            fix_success_count=0, fix_total_count=0, has_history=False,
        )
        return session

    def test_blocks_below_30_percent(self):
        session = self._make_session(0.25)
        result = evaluate_guardrails(session)
        assert not result.passed
        assert any("confidence score" in r.lower() for r in result.blocked_reasons)

    def test_warns_below_50_percent(self):
        session = self._make_session(0.45)
        result = evaluate_guardrails(session)
        assert result.passed  # Not blocked
        assert any("confidence score" in w.lower() for w in result.warnings)

    def test_passes_above_50_percent(self):
        session = self._make_session(0.75)
        result = evaluate_guardrails(session)
        assert result.passed
        # No confidence-related warnings
        assert not any("confidence score" in w.lower() for w in result.warnings)

    def test_no_confidence_no_check(self):
        """If fix_confidence is None, guardrail is skipped."""
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING,
            labels={"namespace": "production"},
        )
        session = DiagnosisSession(alert)
        session.set_diagnosis("test", Confidence.HIGH, ["e"])
        session.set_fix_proposal("fix", "desc", RiskLevel.LOW, "dry", "rollback")
        session.fix_confidence = None
        result = evaluate_guardrails(session)
        assert result.passed
