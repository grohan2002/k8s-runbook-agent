"""Tests for the Triage Agent."""

import pytest

from k8s_runbook_agent.agent.multi_agent.triage import TriageAgent, TriageResult
from k8s_runbook_agent.agent.multi_agent.tool_subsets import SpecialistDomain
from k8s_runbook_agent.models import AlertStatus, GrafanaAlert


class TestTriageResult:
    def test_to_dict(self):
        result = TriageResult(
            domain=SpecialistDomain.POD,
            confidence="high",
            reasoning="OOMKilled termination reason",
            priority="p1",
            source="triage",
        )
        d = result.to_dict()
        assert d["domain"] == "pod"
        assert d["confidence"] == "high"
        assert d["source"] == "triage"

    def test_deterministic_source(self):
        result = TriageResult(
            domain=SpecialistDomain.NETWORK,
            confidence="medium",
            reasoning="Deterministic routing",
            priority="p2",
            source="deterministic",
        )
        assert result.source == "deterministic"


class TestTriageAgentDeterministicFallback:
    """Test the fallback path (no API key = deterministic routing)."""

    @pytest.fixture
    def agent(self):
        return TriageAgent()

    @pytest.mark.asyncio
    async def test_crashloop_routes_to_pod(self, agent):
        alert = GrafanaAlert(
            alert_name="KubePodCrashLooping",
            status=AlertStatus.FIRING,
            labels={"namespace": "production", "pod": "api-xyz", "severity": "critical"},
        )
        result = await agent.classify(alert)
        assert result.domain == SpecialistDomain.POD
        assert result.source == "deterministic"  # No API key in test env
        assert result.priority == "p1"  # critical severity

    @pytest.mark.asyncio
    async def test_dns_routes_to_network(self, agent):
        alert = GrafanaAlert(
            alert_name="CoreDNSDown",
            status=AlertStatus.FIRING,
            labels={"severity": "critical"},
        )
        result = await agent.classify(alert)
        assert result.domain == SpecialistDomain.NETWORK

    @pytest.mark.asyncio
    async def test_node_routes_to_infra(self, agent):
        alert = GrafanaAlert(
            alert_name="KubeNodeNotReady",
            status=AlertStatus.FIRING,
            labels={"node": "worker-1", "severity": "critical"},
        )
        result = await agent.classify(alert)
        assert result.domain == SpecialistDomain.INFRASTRUCTURE

    @pytest.mark.asyncio
    async def test_error_rate_routes_to_app(self, agent):
        alert = GrafanaAlert(
            alert_name="HighErrorRate",
            status=AlertStatus.FIRING,
            labels={"severity": "warning"},
        )
        result = await agent.classify(alert)
        assert result.domain == SpecialistDomain.APPLICATION
        assert result.priority == "p2"  # warning severity

    @pytest.mark.asyncio
    async def test_unknown_alert_routes_by_labels(self, agent):
        alert = GrafanaAlert(
            alert_name="CustomMetricBreached",
            status=AlertStatus.FIRING,
            labels={"pod": "worker-abc", "severity": "info"},
        )
        result = await agent.classify(alert)
        assert result.domain == SpecialistDomain.POD  # has pod label
        assert result.priority == "p3"  # info severity


class TestTriageResponseParsing:
    """Test the JSON parsing of Haiku's response."""

    def test_parse_valid_json(self):
        agent = TriageAgent()
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING,
            labels={"severity": "warning"},
        )
        text = '{"domain": "pod", "confidence": "high", "reasoning": "OOM", "priority": "p1"}'
        result = agent._parse_triage_response(text, alert)
        assert result.domain == SpecialistDomain.POD
        assert result.confidence == "high"

    def test_parse_json_in_code_block(self):
        agent = TriageAgent()
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING,
            labels={"severity": "warning"},
        )
        text = '```json\n{"domain": "network", "confidence": "medium", "reasoning": "DNS", "priority": "p2"}\n```'
        result = agent._parse_triage_response(text, alert)
        assert result.domain == SpecialistDomain.NETWORK

    def test_parse_invalid_json_falls_back(self):
        agent = TriageAgent()
        alert = GrafanaAlert(
            alert_name="KubePodCrashLooping", status=AlertStatus.FIRING,
            labels={"severity": "warning"},
        )
        result = agent._parse_triage_response("not json at all", alert)
        assert result.domain == SpecialistDomain.POD  # deterministic fallback
        assert result.source == "deterministic"

    def test_parse_invalid_domain_falls_back(self):
        agent = TriageAgent()
        alert = GrafanaAlert(
            alert_name="KubePodOOMKilled", status=AlertStatus.FIRING,
            labels={"severity": "critical"},
        )
        text = '{"domain": "invalid_domain", "confidence": "high", "reasoning": "test", "priority": "p1"}'
        result = agent._parse_triage_response(text, alert)
        # Should fall back to deterministic routing for the domain
        assert result.domain == SpecialistDomain.POD
