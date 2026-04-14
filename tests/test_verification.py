"""Tests for the fix verification loop."""

import pytest

from k8s_runbook_agent.agent.session import DiagnosisSession, SessionPhase
from k8s_runbook_agent.agent.verification import (
    VerificationResult,
    VerificationVerdict,
    _build_reviewer_message,
    _parse_verdict,
    extract_tool_results_summary,
)
from k8s_runbook_agent.models import (
    AlertStatus,
    Confidence,
    GrafanaAlert,
    RiskLevel,
)


class TestParseVerdict:
    def test_approve_simple(self):
        result = _parse_verdict("APPROVE")
        assert result.verdict == VerificationVerdict.APPROVE

    def test_approve_with_comment(self):
        result = _parse_verdict("APPROVE - looks good, evidence is solid")
        assert result.verdict == VerificationVerdict.APPROVE

    def test_revise_with_feedback(self):
        result = _parse_verdict("REVISE: The rollback plan is missing. Add kubectl rollout undo command.")
        assert result.verdict == VerificationVerdict.REVISE
        assert "rollback" in result.feedback.lower()

    def test_reject_with_reason(self):
        result = _parse_verdict("REJECT: The evidence shows OOMKilled but the diagnosis says config error. These contradict.")
        assert result.verdict == VerificationVerdict.REJECT
        assert "contradict" in result.feedback.lower()

    def test_case_insensitive(self):
        assert _parse_verdict("approve").verdict == VerificationVerdict.APPROVE
        assert _parse_verdict("Revise: fix it").verdict == VerificationVerdict.REVISE
        assert _parse_verdict("reject: bad").verdict == VerificationVerdict.REJECT

    def test_fallback_on_garbage(self):
        result = _parse_verdict("I think this looks okay maybe?")
        assert result.verdict == VerificationVerdict.APPROVE  # fail-open

    def test_fallback_on_empty(self):
        result = _parse_verdict("")
        assert result.verdict == VerificationVerdict.APPROVE

    def test_multiline_revise(self):
        text = "REVISE: Two issues:\n1. Missing rollback plan\n2. Risk should be MEDIUM not LOW"
        result = _parse_verdict(text)
        assert result.verdict == VerificationVerdict.REVISE
        assert "Missing rollback" in result.feedback


class TestBuildReviewerMessage:
    @pytest.fixture
    def diagnosed_session(self):
        alert = GrafanaAlert(
            alert_name="KubePodCrashLooping",
            status=AlertStatus.FIRING,
            labels={"namespace": "production", "pod": "api-abc", "severity": "critical"},
        )
        session = DiagnosisSession(alert)
        session.set_diagnosis(
            root_cause="OOMKilled — memory limit 256Mi too low",
            confidence=Confidence.HIGH,
            evidence=["Exit code 137", "Memory at 254Mi/256Mi"],
            ruled_out=["Image pull failure"],
        )
        session.set_fix_proposal(
            summary="Increase memory to 512Mi",
            description="Patch deployment to set memory limit to 512Mi",
            risk_level=RiskLevel.LOW,
            dry_run_output="memory: 256Mi -> 512Mi",
            rollback_plan="kubectl rollout undo deployment/api -n production",
        )
        return session

    def test_contains_alert_info(self, diagnosed_session):
        msg = _build_reviewer_message(diagnosed_session, "")
        assert "KubePodCrashLooping" in msg
        assert "production" in msg
        assert "api-abc" in msg

    def test_contains_diagnosis(self, diagnosed_session):
        msg = _build_reviewer_message(diagnosed_session, "")
        assert "OOMKilled" in msg
        assert "high" in msg  # confidence enum value is lowercase
        assert "Exit code 137" in msg

    def test_contains_fix_proposal(self, diagnosed_session):
        msg = _build_reviewer_message(diagnosed_session, "")
        assert "512Mi" in msg
        assert "low" in msg  # risk_level enum value is lowercase
        assert "rollout undo" in msg

    def test_contains_tool_results(self, diagnosed_session):
        msg = _build_reviewer_message(diagnosed_session, "### get_pod_status\nRunning, 5 restarts")
        assert "get_pod_status" in msg
        assert "5 restarts" in msg

    def test_missing_rollback_noted(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={"severity": "warning"},
        )
        session = DiagnosisSession(alert)
        session.set_diagnosis("test", Confidence.HIGH, [])
        session.set_fix_proposal("fix", "desc", RiskLevel.LOW, rollback_plan="")
        msg = _build_reviewer_message(session, "")
        assert "NONE PROVIDED" in msg


class TestExtractToolResultsSummary:
    def test_extracts_results(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={},
        )
        session = DiagnosisSession(alert)

        # Simulate assistant message with tool_use
        session.messages.append({
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "get_pod_status", "input": {}},
            ],
        })
        # Simulate tool result
        session.messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "Pod is CrashLoopBackOff"},
            ],
        })

        summary = extract_tool_results_summary(session)
        assert "get_pod_status" in summary
        assert "CrashLoopBackOff" in summary

    def test_truncates_long_results(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={},
        )
        session = DiagnosisSession(alert)

        session.messages.append({
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "get_pod_logs", "input": {}}],
        })
        session.messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 5000}],
        })

        summary = extract_tool_results_summary(session)
        assert len(summary) < 5000  # Should be truncated

    def test_empty_messages(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={},
        )
        session = DiagnosisSession(alert)
        summary = extract_tool_results_summary(session)
        assert "no tool results" in summary.lower()

    def test_handles_string_content_messages(self):
        """Messages with string content (not list) should be skipped gracefully."""
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={},
        )
        session = DiagnosisSession(alert)
        session.messages.append({"role": "user", "content": "plain text message"})
        summary = extract_tool_results_summary(session)
        assert "no tool results" in summary.lower()
