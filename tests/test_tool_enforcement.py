"""Tests for tool-call enforcement — prevents premature diagnosis."""

import pytest

from k8s_runbook_agent.agent.multi_agent.tool_subsets import (
    GENERAL_REQUIRED_EITHER,
    GENERAL_REQUIRED_TOOLS,
    REQUIRED_TOOLS,
    SpecialistDomain,
    check_required_tools_met,
)
from k8s_runbook_agent.agent.session import DiagnosisSession
from k8s_runbook_agent.models import AlertStatus, GrafanaAlert


class TestCheckRequiredToolsMet:
    def test_pod_all_present(self):
        met, missing = check_required_tools_met(
            SpecialistDomain.POD,
            {"get_pod_status", "get_pod_logs", "get_events", "describe_resource"},
        )
        assert met is True
        assert missing == set()

    def test_pod_missing_logs(self):
        met, missing = check_required_tools_met(
            SpecialistDomain.POD,
            {"get_pod_status", "get_events"},
        )
        assert met is False
        assert "get_pod_logs" in missing

    def test_network_all_present(self):
        met, missing = check_required_tools_met(
            SpecialistDomain.NETWORK,
            {"get_endpoint_status", "get_events", "describe_resource"},
        )
        assert met is True

    def test_network_missing_two(self):
        met, missing = check_required_tools_met(
            SpecialistDomain.NETWORK,
            {"get_events"},
        )
        assert met is False
        assert len(missing) == 2

    def test_infrastructure_all_present(self):
        met, missing = check_required_tools_met(
            SpecialistDomain.INFRASTRUCTURE,
            {"get_node_conditions", "get_events", "get_resource_usage"},
        )
        assert met is True

    def test_application_all_present(self):
        met, missing = check_required_tools_met(
            SpecialistDomain.APPLICATION,
            {"get_pod_status", "get_pod_logs", "get_endpoint_status"},
        )
        assert met is True


class TestGeneralModeEnforcement:
    def test_general_all_met(self):
        met, missing = check_required_tools_met(
            None,
            {"get_events", "get_pod_status"},
        )
        assert met is True

    def test_general_missing_events(self):
        met, missing = check_required_tools_met(
            None,
            {"get_pod_status"},
        )
        assert met is False
        assert "get_events" in missing

    def test_general_either_pod_status(self):
        met, missing = check_required_tools_met(
            None,
            {"get_events", "get_pod_status"},
        )
        assert met is True

    def test_general_either_node_conditions(self):
        met, missing = check_required_tools_met(
            None,
            {"get_events", "get_node_conditions"},
        )
        assert met is True

    def test_general_missing_either(self):
        met, missing = check_required_tools_met(
            None,
            {"get_events"},  # has events but not pod_status or node_conditions
        )
        assert met is False
        # Should mention the either-or requirement
        assert any("one of" in str(m) for m in missing)

    def test_empty_tools_called(self):
        met, missing = check_required_tools_met(None, set())
        assert met is False
        assert len(missing) >= 2


class TestToolsCalledTracking:
    def test_session_tools_called_default(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={},
        )
        session = DiagnosisSession(alert)
        assert session.tools_called == set()
        assert session.tool_calls_made == 0

    def test_session_tools_called_add(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={},
        )
        session = DiagnosisSession(alert)
        session.tools_called.add("get_pod_status")
        session.tools_called.add("get_pod_logs")
        session.tools_called.add("get_pod_status")  # duplicate
        assert len(session.tools_called) == 2
        assert "get_pod_status" in session.tools_called


class TestEnforcementBounds:
    def test_required_tools_all_exist_in_domain_tools(self):
        """Verify all required tools are actually in the domain's tool set."""
        from k8s_runbook_agent.agent.multi_agent.tool_subsets import DOMAIN_TOOLS

        for domain, required in REQUIRED_TOOLS.items():
            available = set(DOMAIN_TOOLS[domain])
            missing = required - available
            assert not missing, f"Domain {domain}: required tools {missing} not in available set"
