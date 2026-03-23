"""Tests for specialist agents."""

import pytest

from k8s_runbook_agent.agent.multi_agent.specialists import (
    DOMAIN_PROMPTS,
    SpecialistAgent,
    get_specialist,
)
from k8s_runbook_agent.agent.multi_agent.tool_subsets import (
    MUTATION_TOOLS,
    SpecialistDomain,
    get_domain_tool_names,
)
from k8s_runbook_agent.agent.multi_agent.triage import TriageResult
from k8s_runbook_agent.models import AlertStatus, GrafanaAlert


class TestSpecialistAgent:
    def test_all_domains_have_prompts(self):
        for domain in SpecialistDomain:
            assert domain in DOMAIN_PROMPTS, f"Missing prompt for {domain.value}"
            assert len(DOMAIN_PROMPTS[domain]) > 200, f"Prompt too short for {domain.value}"

    def test_creates_filtered_registry(self):
        agent = SpecialistAgent(SpecialistDomain.POD)
        registry = agent.registry
        expected_tools = set(get_domain_tool_names(SpecialistDomain.POD))
        assert set(registry.tool_names) == expected_tools

    def test_no_mutation_tools(self):
        for domain in SpecialistDomain:
            agent = SpecialistAgent(domain)
            for tool_name in agent.registry.tool_names:
                assert tool_name not in MUTATION_TOOLS, (
                    f"{domain.value} specialist has mutation tool: {tool_name}"
                )

    def test_builds_system_prompt(self):
        agent = SpecialistAgent(SpecialistDomain.NETWORK)
        alert = GrafanaAlert(
            alert_name="CoreDNSDown", status=AlertStatus.FIRING,
            labels={"severity": "critical"},
        )
        triage = TriageResult(
            domain=SpecialistDomain.NETWORK,
            confidence="high",
            reasoning="DNS failure detected",
            priority="p1",
        )
        prompt = agent.build_system_prompt(alert, triage)
        assert "NetDiag" in prompt or "network" in prompt.lower()
        assert "DNS failure detected" in prompt
        assert "CoreDNSDown" in prompt
        assert "diagnosis" in prompt.lower()  # output format included

    def test_builds_prompt_with_memory(self):
        agent = SpecialistAgent(SpecialistDomain.POD)
        alert = GrafanaAlert(
            alert_name="KubePodOOMKilled", status=AlertStatus.FIRING,
            labels={"severity": "critical"},
        )
        triage = TriageResult(
            domain=SpecialistDomain.POD,
            confidence="high",
            reasoning="OOM",
            priority="p1",
        )
        memory = "## Past Incidents\n1. OOMKilled 3 times this week"
        prompt = agent.build_system_prompt(alert, triage, memory_context=memory)
        assert "Past Incidents" in prompt
        assert "OOMKilled 3 times" in prompt

    def test_builds_opening_message(self):
        agent = SpecialistAgent(SpecialistDomain.INFRASTRUCTURE)
        alert = GrafanaAlert(
            alert_name="KubeNodeNotReady", status=AlertStatus.FIRING,
            labels={"node": "worker-1", "severity": "critical", "namespace": "default"},
        )
        triage = TriageResult(
            domain=SpecialistDomain.INFRASTRUCTURE,
            confidence="high",
            reasoning="Node issue",
            priority="p1",
        )
        msg = agent._build_opening(alert, triage)
        assert "infrastructure specialist" in msg
        assert "KubeNodeNotReady" in msg


class TestGetSpecialist:
    def test_returns_singleton(self):
        s1 = get_specialist(SpecialistDomain.POD)
        s2 = get_specialist(SpecialistDomain.POD)
        assert s1 is s2

    def test_different_domains_different_instances(self):
        pod = get_specialist(SpecialistDomain.POD)
        net = get_specialist(SpecialistDomain.NETWORK)
        assert pod is not net
        assert pod.domain != net.domain

    def test_all_domains_instantiate(self):
        for domain in SpecialistDomain:
            agent = get_specialist(domain)
            assert agent.domain == domain
            assert len(agent.registry.tool_names) >= 8


class TestSpecialistPromptOutputFormat:
    """Verify that every specialist prompt includes the output format block."""

    @pytest.mark.parametrize("domain", list(SpecialistDomain))
    def test_prompt_contains_output_format_placeholder(self, domain):
        prompt = DOMAIN_PROMPTS[domain]
        assert "{output_format}" in prompt, (
            f"{domain.value} prompt missing {{output_format}} placeholder"
        )

    @pytest.mark.parametrize("domain", list(SpecialistDomain))
    def test_formatted_prompt_contains_diagnosis_block(self, domain):
        from k8s_runbook_agent.agent.prompts import OUTPUT_FORMAT

        prompt = DOMAIN_PROMPTS[domain].format(output_format=OUTPUT_FORMAT)
        assert "```diagnosis" in prompt
        assert "ROOT_CAUSE" in prompt
        assert "```fix_proposal" in prompt
        assert "```escalate" in prompt
