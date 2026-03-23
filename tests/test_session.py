"""Tests for session state management and lifecycle transitions."""

import pytest

from k8s_runbook_agent.agent.session import DiagnosisSession, SessionPhase, SessionStore
from k8s_runbook_agent.models import (
    AlertStatus,
    ApprovalStatus,
    Confidence,
    GrafanaAlert,
    RiskLevel,
)


class TestSessionLifecycle:
    def test_initial_state(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        assert session.phase == SessionPhase.ALERT_RECEIVED
        assert session.id.startswith("diag-")
        assert session.tool_calls_made == 0
        assert session.total_tokens_used == 0
        assert session.diagnosis is None
        assert session.fix_proposal is None
        assert session.error is None

    def test_transition_to_investigating(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.transition(SessionPhase.INVESTIGATING)
        assert session.phase == SessionPhase.INVESTIGATING

    def test_set_diagnosis_transitions(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.set_diagnosis(
            root_cause="OOMKilled",
            confidence=Confidence.HIGH,
            evidence=["exit 137"],
        )
        assert session.phase == SessionPhase.DIAGNOSED
        assert session.diagnosis.root_cause == "OOMKilled"
        assert session.diagnosis.confidence == Confidence.HIGH

    def test_set_fix_proposal_transitions(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.set_diagnosis("test", Confidence.HIGH, [])
        session.set_fix_proposal(
            summary="Fix it",
            description="Do the thing",
            risk_level=RiskLevel.LOW,
        )
        assert session.phase == SessionPhase.FIX_PROPOSED
        assert session.fix_proposal.summary == "Fix it"

    def test_approval_flow(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.set_diagnosis("test", Confidence.HIGH, [])
        session.set_fix_proposal("fix", "desc", RiskLevel.LOW)
        session.request_approval()
        assert session.phase == SessionPhase.AWAITING_APPROVAL

        session.approve("user123")
        assert session.phase == SessionPhase.EXECUTING
        assert session.approval.status == ApprovalStatus.APPROVED
        assert session.approval.approved_by == "user123"
        assert session.approval.approved_at is not None

    def test_rejection_flow(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.set_diagnosis("test", Confidence.HIGH, [])
        session.set_fix_proposal("fix", "desc", RiskLevel.LOW)
        session.request_approval()
        session.reject("user456")
        assert session.phase == SessionPhase.RESOLVED
        assert session.approval.status == ApprovalStatus.REJECTED

    def test_escalation(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.escalate("Cannot determine root cause")
        assert session.phase == SessionPhase.ESCALATED
        assert session.error == "Cannot determine root cause"

    def test_failure(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.fail("API error")
        assert session.phase == SessionPhase.FAILED
        assert session.error == "API error"

    def test_mark_resolved(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.mark_resolved("Fix applied successfully")
        assert session.phase == SessionPhase.RESOLVED
        assert session.approval.executed is True
        assert session.approval.execution_result == "Fix applied successfully"

    def test_summary_text(self, sample_session):
        text = sample_session.summary_text()
        assert sample_session.id in text
        assert "KubePodCrashLooping" in text
        assert "OOMKilled" in text


class TestSessionStore:
    def test_create_and_get(self, clean_session_store, sample_alert):
        session = clean_session_store.create(sample_alert)
        assert clean_session_store.get(session.id) is session

    def test_get_nonexistent(self, clean_session_store):
        assert clean_session_store.get("diag-nonexistent") is None

    def test_get_by_fingerprint(self, clean_session_store, sample_alert):
        session = clean_session_store.create(sample_alert)
        found = clean_session_store.get_by_fingerprint("fp-crash-001")
        assert found is session

    def test_fingerprint_ignores_resolved(self, clean_session_store, sample_alert):
        session = clean_session_store.create(sample_alert)
        session.mark_resolved("done")
        found = clean_session_store.get_by_fingerprint("fp-crash-001")
        assert found is None

    def test_active_sessions(self, clean_session_store, sample_alert, sample_oom_alert):
        s1 = clean_session_store.create(sample_alert)
        s2 = clean_session_store.create(sample_oom_alert)
        assert len(clean_session_store.active_sessions()) == 2

        s1.mark_resolved("done")
        assert len(clean_session_store.active_sessions()) == 1

    def test_all_sessions(self, clean_session_store, sample_alert):
        clean_session_store.create(sample_alert)
        assert len(clean_session_store.all_sessions()) == 1


class TestConversationManagement:
    def test_add_messages(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.add_user_message("Hello")
        session.add_assistant_message("Hi there")
        assert len(session.messages) == 2
        assert session.messages[0]["role"] == "user"
        assert session.messages[1]["role"] == "assistant"

    def test_add_tool_result(self, sample_alert):
        session = DiagnosisSession(sample_alert)
        session.add_tool_result("tool-123", "result data", is_error=False)
        assert session.tool_calls_made == 1
        assert session.messages[0]["role"] == "user"
        content = session.messages[0]["content"][0]
        assert content["type"] == "tool_result"
        assert content["tool_use_id"] == "tool-123"
