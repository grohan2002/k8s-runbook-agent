"""Tests for domain tool subsets."""

import pytest

from k8s_runbook_agent.agent.multi_agent.tool_subsets import (
    DOMAIN_TOOLS,
    MUTATION_TOOLS,
    SpecialistDomain,
    get_domain_tool_names,
    validate_no_mutations,
)
from k8s_runbook_agent.agent.tool_registry import build_default_registry, build_domain_registry


class TestToolSubsets:
    def test_all_domains_have_tools(self):
        for domain in SpecialistDomain:
            tools = get_domain_tool_names(domain)
            assert len(tools) >= 8, f"{domain.value} has too few tools: {len(tools)}"

    def test_no_domain_has_mutation_tools(self):
        for domain in SpecialistDomain:
            tools = get_domain_tool_names(domain)
            validate_no_mutations(tools)  # raises if mutation tools leak

    def test_all_domains_have_common_tools(self):
        """Every specialist should have events, describe, yaml, list, runbooks."""
        required = {"get_events", "describe_resource", "get_resource_yaml", "list_resources", "search_runbooks", "get_runbook"}
        for domain in SpecialistDomain:
            tools = set(get_domain_tool_names(domain))
            missing = required - tools
            assert not missing, f"{domain.value} missing common tools: {missing}"

    def test_pod_has_pod_tools(self):
        tools = set(get_domain_tool_names(SpecialistDomain.POD))
        assert "get_pod_status" in tools
        assert "get_pod_logs" in tools
        assert "get_resource_usage" in tools

    def test_network_has_network_tools(self):
        tools = set(get_domain_tool_names(SpecialistDomain.NETWORK))
        assert "get_endpoint_status" in tools
        assert "get_ingress_status" in tools
        assert "get_network_policy" in tools

    def test_infra_has_infra_tools(self):
        tools = set(get_domain_tool_names(SpecialistDomain.INFRASTRUCTURE))
        assert "get_node_conditions" in tools
        assert "get_hpa_status" in tools
        assert "get_pvc_status" in tools

    def test_app_has_broad_tools(self):
        tools = set(get_domain_tool_names(SpecialistDomain.APPLICATION))
        assert "get_pod_status" in tools
        assert "get_pod_logs" in tools
        assert "get_endpoint_status" in tools
        assert "get_hpa_status" in tools

    def test_all_domain_tools_exist_in_registry(self):
        """Verify every tool name in DOMAIN_TOOLS actually exists."""
        registry = build_default_registry()
        all_tool_names = set(registry.tool_names)

        for domain in SpecialistDomain:
            for tool_name in get_domain_tool_names(domain):
                assert tool_name in all_tool_names, (
                    f"Domain {domain.value} references non-existent tool: {tool_name}"
                )

    def test_no_duplicate_tools_per_domain(self):
        for domain in SpecialistDomain:
            tools = get_domain_tool_names(domain)
            assert len(tools) == len(set(tools)), f"{domain.value} has duplicate tools"


class TestBuildDomainRegistry:
    def test_builds_filtered_registry(self):
        tools = get_domain_tool_names(SpecialistDomain.POD)
        registry = build_domain_registry(tools)
        assert set(registry.tool_names) == set(tools)

    def test_filtered_registry_generates_schemas(self):
        tools = get_domain_tool_names(SpecialistDomain.NETWORK)
        registry = build_domain_registry(tools)
        schemas = registry.to_anthropic_tools()
        assert len(schemas) == len(tools)
        for schema in schemas:
            assert schema["name"] in tools

    @pytest.mark.asyncio
    async def test_filtered_registry_dispatches(self):
        tools = get_domain_tool_names(SpecialistDomain.POD)
        registry = build_domain_registry(tools)

        # Can dispatch included tools
        result = await registry.dispatch("search_runbooks", {"query": "test"})
        assert not result.get("is_error", False)

        # Cannot dispatch excluded tools
        result = await registry.dispatch("patch_resource", {"name": "test"})
        assert result["is_error"] is True
        assert "Unknown tool" in result["content"][0]["text"]

    def test_mutation_tools_not_in_any_domain_registry(self):
        for domain in SpecialistDomain:
            tools = get_domain_tool_names(domain)
            registry = build_domain_registry(tools)
            for mut_tool in MUTATION_TOOLS:
                assert mut_tool not in registry.tool_names


class TestValidateNoMutations:
    def test_raises_on_mutation_leak(self):
        with pytest.raises(ValueError, match="Mutation tools leaked"):
            validate_no_mutations(["get_pod_status", "patch_resource"])

    def test_passes_on_clean_list(self):
        validate_no_mutations(["get_pod_status", "get_events"])  # no exception
