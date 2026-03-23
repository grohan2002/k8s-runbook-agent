"""Tests for the tool registry."""

import pytest

from k8s_runbook_agent.agent.tool_registry import ToolRegistry, build_default_registry


class TestToolRegistry:
    @pytest.fixture
    def registry(self):
        return build_default_registry()

    def test_loads_all_tools(self, registry):
        names = registry.tool_names
        assert len(names) == 22  # 14 inspect + 2 knowledge + 6 mutate

    def test_inspection_tools_present(self, registry):
        expected = [
            "get_pod_status", "get_pod_logs", "get_events", "get_resource_usage",
            "describe_resource", "get_resource_yaml", "list_resources",
            "check_resource_exists", "get_endpoint_status", "get_node_conditions",
            "get_hpa_status", "get_pvc_status", "get_ingress_status",
            "get_network_policy",
        ]
        for name in expected:
            assert name in registry.tool_names, f"Missing inspection tool: {name}"

    def test_mutation_tools_present(self, registry):
        expected = [
            "patch_resource", "scale_deployment", "rollback_deployment",
            "restart_deployment", "delete_pod", "create_resource",
        ]
        for name in expected:
            assert name in registry.tool_names, f"Missing mutation tool: {name}"

    def test_knowledge_tools_present(self, registry):
        assert "search_runbooks" in registry.tool_names
        assert "get_runbook" in registry.tool_names

    def test_generates_anthropic_schema(self, registry):
        tools = registry.to_anthropic_tools()
        assert len(tools) == 22
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"

    def test_mutation_tools_have_dry_run(self, registry):
        tools = registry.to_anthropic_tools()
        mutation_names = {
            "patch_resource", "scale_deployment", "rollback_deployment",
            "restart_deployment", "delete_pod", "create_resource",
        }
        for tool in tools:
            if tool["name"] in mutation_names:
                props = tool["input_schema"]["properties"]
                assert "dry_run" in props, f"{tool['name']} missing dry_run param"

    @pytest.mark.asyncio
    async def test_dispatch_unknown_tool(self, registry):
        result = await registry.dispatch("nonexistent_tool", {})
        assert result["is_error"] is True
        assert "Unknown tool" in result["content"][0]["text"]


class TestEmptyRegistry:
    def test_register_and_dispatch(self):
        reg = ToolRegistry()

        async def dummy_handler(args):
            return {"content": [{"type": "text", "text": f"got: {args}"}]}

        reg.register(
            name="test_tool",
            description="A test tool",
            parameters={"input": str},
            handler=dummy_handler,
        )

        assert "test_tool" in reg.tool_names
        tools = reg.to_anthropic_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "test_tool"
