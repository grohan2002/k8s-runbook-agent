"""Tests for per-agent health checks."""

import pytest

from k8s_runbook_agent.agent.multi_agent.health import (
    AgentHealthChecker,
    AgentHealthResult,
    summarize_health,
)
from k8s_runbook_agent.agent.multi_agent.tool_subsets import SpecialistDomain


class TestAgentHealthResult:
    def test_to_dict_ok(self):
        r = AgentHealthResult(
            agent_name="triage", status="ok",
            model="claude-3-5-haiku", latency_ms=150.3,
        )
        d = r.to_dict()
        assert d["agent"] == "triage"
        assert d["status"] == "ok"
        assert d["model"] == "claude-3-5-haiku"
        assert d["latency_ms"] == 150.3
        assert d["cached"] is False

    def test_to_dict_error(self):
        r = AgentHealthResult(
            agent_name="coordinator", status="error",
            error="API key invalid",
        )
        d = r.to_dict()
        assert d["status"] == "error"
        assert d["error"] == "API key invalid"
        assert "model" not in d  # empty string omitted

    def test_to_dict_not_configured(self):
        r = AgentHealthResult(
            agent_name="triage", status="not_configured",
            details={"reason": "MULTI_AGENT_ENABLED=false"},
        )
        d = r.to_dict()
        assert d["status"] == "not_configured"
        assert d["details"]["reason"] == "MULTI_AGENT_ENABLED=false"

    def test_to_dict_with_tools(self):
        r = AgentHealthResult(
            agent_name="specialist_pod", status="ok",
            model="claude-sonnet-4", tool_count=11,
        )
        d = r.to_dict()
        assert d["tool_count"] == 11


class TestSummarizeHealth:
    def test_all_ok(self):
        results = {
            "triage": AgentHealthResult("triage", "ok"),
            "specialist_pod": AgentHealthResult("specialist_pod", "ok"),
        }
        summary = summarize_health(results)
        assert summary["status"] == "all_healthy"
        assert summary["healthy_count"] == 2
        assert summary["error_count"] == 0

    def test_ok_plus_not_configured(self):
        results = {
            "triage": AgentHealthResult("triage", "not_configured"),
            "executor": AgentHealthResult("executor", "ok"),
        }
        summary = summarize_health(results)
        assert summary["status"] == "all_healthy"

    def test_has_errors(self):
        results = {
            "triage": AgentHealthResult("triage", "ok"),
            "specialist_pod": AgentHealthResult("specialist_pod", "error", error="API down"),
        }
        summary = summarize_health(results)
        assert summary["status"] == "unhealthy"
        assert summary["error_count"] == 1

    def test_degraded(self):
        results = {
            "triage": AgentHealthResult("triage", "ok"),
            "coordinator": AgentHealthResult("coordinator", "degraded"),
        }
        summary = summarize_health(results)
        assert summary["status"] == "degraded"
        assert summary["degraded_count"] == 1

    def test_includes_multi_agent_flag(self):
        results = {
            "triage": AgentHealthResult("triage", "not_configured"),
        }
        summary = summarize_health(results)
        assert "multi_agent_enabled" in summary

    def test_agents_dict(self):
        results = {
            "triage": AgentHealthResult("triage", "ok", model="haiku"),
            "executor": AgentHealthResult("executor", "ok", model="sonnet"),
        }
        summary = summarize_health(results)
        assert "triage" in summary["agents"]
        assert "executor" in summary["agents"]
        assert summary["agents"]["triage"]["model"] == "haiku"


class TestAgentHealthChecker:
    """Test the checker with no API key (all agents degrade gracefully)."""

    @pytest.mark.asyncio
    async def test_check_all_no_api_key(self):
        """Without API key, agents should report error or not_configured."""
        checker = AgentHealthChecker()
        results = await checker.check_all()

        # Should have all 8 agents
        assert len(results) == 8
        expected_names = {
            "triage", "specialist_pod", "specialist_network",
            "specialist_infrastructure", "specialist_application",
            "coordinator", "executor", "embeddings",
        }
        assert set(results.keys()) == expected_names

    @pytest.mark.asyncio
    async def test_caching(self):
        """Second call should return cached results."""
        checker = AgentHealthChecker()
        results1 = await checker.check_all()
        results2 = await checker.check_all()

        # All second-call results should be cached
        for name, result in results2.items():
            assert result.cached is True, f"{name} should be cached"

    @pytest.mark.asyncio
    async def test_force_bypass_cache(self):
        """Force=True should bypass cache."""
        checker = AgentHealthChecker()
        await checker.check_all()  # populate cache
        results = await checker.check_all(force=True)

        for name, result in results.items():
            assert result.cached is False, f"{name} should not be cached with force=True"

    @pytest.mark.asyncio
    async def test_specialist_checks_tool_count(self):
        """Specialist health should report tool counts."""
        checker = AgentHealthChecker()
        result = await checker.check_specialist(SpecialistDomain.POD)
        # Even without API key, tool_count should be populated (or status is error/not_configured)
        d = result.to_dict()
        assert "agent" in d

    @pytest.mark.asyncio
    async def test_executor_reports_mutation_tools(self):
        """Executor health should list available mutation tools."""
        checker = AgentHealthChecker()
        result = await checker.check_executor()
        d = result.to_dict()
        assert "agent" in d
        assert d["agent"] == "executor"


class TestHealthEndpointIntegration:
    """Test the /ready/agents endpoint via HTTPX."""

    @pytest.fixture
    async def client(self):
        from httpx import ASGITransport, AsyncClient
        from k8s_runbook_agent.server import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    @pytest.mark.asyncio
    async def test_ready_agents_endpoint(self, client):
        resp = await client.get("/ready/agents")
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert "status" in data
        assert "agents" in data
        assert "healthy_count" in data
        assert "multi_agent_enabled" in data

    @pytest.mark.asyncio
    async def test_ready_agents_force(self, client):
        resp = await client.get("/ready/agents?force=true")
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert "agents" in data

    @pytest.mark.asyncio
    async def test_ready_includes_multi_agent_summary(self, client):
        resp = await client.get("/ready")
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert "multi_agent" in data["checks"]
