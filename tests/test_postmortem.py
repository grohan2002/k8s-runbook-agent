"""Tests for the post-mortem generator."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from k8s_runbook_agent.agent.postmortem import (
    _build_fix_applied,
    _build_metadata,
    _build_root_cause,
    _build_summary,
    generate_postmortem,
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
def _build_resolved_session() -> DiagnosisSession:
    alert = GrafanaAlert(
        alert_name="KubePodCrashLooping",
        status=AlertStatus.FIRING,
        labels={"namespace": "production", "pod": "api-abc", "severity": "critical"},
        fingerprint="fp-pm-001",
    )
    session = DiagnosisSession(alert)
    session.set_diagnosis(
        root_cause="OOMKilled — memory limit 256Mi too low",
        confidence=Confidence.HIGH,
        evidence=["Exit code 137", "Memory at 254Mi/256Mi"],
        ruled_out=["Image pull failure"],
    )
    session.set_fix_proposal(
        summary="Increase memory limit to 512Mi",
        description="Patch deployment to set memory limit to 512Mi",
        risk_level=RiskLevel.LOW,
        dry_run_output="memory: 256Mi -> 512Mi",
        rollback_plan="kubectl rollout undo deployment/api -n production",
    )
    session.request_approval()
    session.approve("sre@example.com")
    session.mark_resolved("Fix applied successfully; memory now at 340Mi/512Mi")
    session.tool_calls_made = 5
    session.tools_called = {"get_pod_status", "get_pod_logs", "get_events"}
    session.total_tokens_used = 15000
    return session


# ---------------------------------------------------------------------------
# Section tests
# ---------------------------------------------------------------------------
class TestBuildSummary:
    def test_resolved_outcome(self):
        session = _build_resolved_session()
        summary = _build_summary(session)
        assert "Resolved" in summary
        assert "KubePodCrashLooping" in summary
        assert "production" in summary
        assert "api-abc" in summary

    def test_escalated_outcome(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={"namespace": "ns"})
        session = DiagnosisSession(alert)
        session.escalate("Cannot determine cause")
        summary = _build_summary(session)
        assert "Escalated" in summary

    def test_failed_outcome(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={"namespace": "ns"})
        session = DiagnosisSession(alert)
        session.fail("API error")
        summary = _build_summary(session)
        assert "Failed" in summary

    def test_slo_included_when_annotated(self):
        alert = GrafanaAlert(
            alert_name="Test",
            status=AlertStatus.FIRING,
            labels={"namespace": "ns"},
            annotations={"slo_name": "api-slo", "error_budget_remaining": "5.5"},
        )
        session = DiagnosisSession(alert)
        session.mark_resolved("done")
        summary = _build_summary(session)
        assert "api-slo" in summary
        assert "5.5%" in summary


class TestBuildRootCause:
    def test_with_diagnosis(self):
        session = _build_resolved_session()
        rc = _build_root_cause(session)
        assert "OOMKilled" in rc
        assert "HIGH" in rc
        assert "Exit code 137" in rc

    def test_no_diagnosis_failed(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={"namespace": "ns"})
        session = DiagnosisSession(alert)
        session.fail("Anthropic API error")
        rc = _build_root_cause(session)
        assert "No diagnosis" in rc
        assert "Anthropic API error" in rc

    def test_no_diagnosis_escalated(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={"namespace": "ns"})
        session = DiagnosisSession(alert)
        session.escalate("Too many possibilities")
        rc = _build_root_cause(session)
        assert "Too many possibilities" in rc


class TestBuildFixApplied:
    def test_approved_and_executed(self):
        session = _build_resolved_session()
        fix = _build_fix_applied(session)
        assert "512Mi" in fix
        assert "LOW" in fix
        assert "sre@example.com" in fix
        assert "successfully" in fix.lower()

    def test_no_fix_proposal(self):
        alert = GrafanaAlert(alert_name="Test", status=AlertStatus.FIRING, labels={"namespace": "ns"})
        session = DiagnosisSession(alert)
        session.escalate("no fix")
        fix = _build_fix_applied(session)
        assert "No fix" in fix


class TestBuildMetadata:
    def test_includes_session_details(self):
        session = _build_resolved_session()
        meta = _build_metadata(session)
        assert session.id in meta
        assert "5" in meta  # tool calls
        assert "15,000" in meta  # tokens

    def test_slack_url_when_thread_ts_present(self):
        session = _build_resolved_session()
        session.slack_channel = "C12345"
        session.slack_thread_ts = "1234567890.123456"
        meta = _build_metadata(session)
        assert "slack.com/archives/C12345" in meta

    def test_incident_ids_listed(self):
        session = _build_resolved_session()
        session.incident_ids = {"pagerduty": "PD-999", "opsgenie": "OG-123"}
        meta = _build_metadata(session)
        assert "pagerduty" in meta
        assert "PD-999" in meta
        assert "opsgenie" in meta


class TestGeneratePostmortem:
    @pytest.mark.asyncio
    async def test_full_markdown_generation(self):
        session = _build_resolved_session()

        # Mock DB + incident memory to avoid external calls
        with patch(
            "k8s_runbook_agent.db.get_audit_log",
            return_value=[],
        ), patch(
            "k8s_runbook_agent.agent.incident_memory.incident_memory.recall",
        ) as mock_recall:
            from k8s_runbook_agent.agent.incident_memory import MemoryContext
            mock_recall.return_value = MemoryContext()
            md = await generate_postmortem(session)

        assert "# Post-Mortem:" in md
        assert "## Timeline" in md
        assert "## Root Cause" in md
        assert "## Fix Applied" in md
        assert "## Metadata" in md
        assert session.id in md

    @pytest.mark.asyncio
    async def test_handles_audit_log_failure(self):
        session = _build_resolved_session()
        with patch(
            "k8s_runbook_agent.db.get_audit_log",
            side_effect=Exception("DB down"),
        ), patch(
            "k8s_runbook_agent.agent.incident_memory.incident_memory.recall",
        ) as mock_recall:
            from k8s_runbook_agent.agent.incident_memory import MemoryContext
            mock_recall.return_value = MemoryContext()
            md = await generate_postmortem(session)
        # Should still return a valid markdown doc
        assert "# Post-Mortem:" in md
        assert "Session created" in md  # fallback timeline

    @pytest.mark.asyncio
    async def test_handles_failed_session(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={"namespace": "ns"},
        )
        session = DiagnosisSession(alert)
        session.fail("Fatal error")

        with patch(
            "k8s_runbook_agent.db.get_audit_log",
            return_value=[],
        ), patch(
            "k8s_runbook_agent.agent.incident_memory.incident_memory.recall",
        ) as mock_recall:
            from k8s_runbook_agent.agent.incident_memory import MemoryContext
            mock_recall.return_value = MemoryContext()
            md = await generate_postmortem(session)

        assert "Failed" in md
        assert "Fatal error" in md
