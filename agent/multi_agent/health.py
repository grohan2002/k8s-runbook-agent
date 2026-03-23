"""Per-agent health checks for the multi-agent system.

Each agent is probed independently so operators can see exactly which
component is degraded. The checks verify:

  1. API key is configured
  2. Model is reachable (lightweight ping — cached 60s)
  3. Tool registry has the expected tool count
  4. Fallback path is available

Health states:
  - ok:             Agent is fully operational
  - degraded:       Agent works but with caveats (e.g. fallback active)
  - error:          Agent cannot function
  - not_configured: Agent is disabled by config
  - unchecked:      Check hasn't run yet (first call)

Results are cached per-agent for 60 seconds to avoid hammering the API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from ...config import settings
from .tool_subsets import SpecialistDomain, get_domain_tool_names

logger = logging.getLogger(__name__)

HEALTH_CACHE_TTL = 60  # seconds


@dataclass
class AgentHealthResult:
    """Health status for a single agent."""

    agent_name: str
    status: str              # ok, degraded, error, not_configured
    model: str = ""
    tool_count: int = 0
    latency_ms: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "agent": self.agent_name,
            "status": self.status,
        }
        if self.model:
            d["model"] = self.model
        if self.tool_count:
            d["tool_count"] = self.tool_count
        if self.latency_ms:
            d["latency_ms"] = round(self.latency_ms, 1)
        if self.error:
            d["error"] = self.error
        if self.details:
            d["details"] = self.details
        d["cached"] = self.cached
        return d


# ---------------------------------------------------------------------------
# Per-agent health checkers
# ---------------------------------------------------------------------------
class AgentHealthChecker:
    """Runs health checks for all agents in the multi-agent system.

    Checks are cached for 60s per agent. Force refresh with `check_all(force=True)`.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, AgentHealthResult]] = {}

    async def check_all(self, force: bool = False) -> dict[str, AgentHealthResult]:
        """Run health checks for all agents. Returns {agent_name: result}."""
        checks = await asyncio.gather(
            self.check_triage(force=force),
            self.check_specialist(SpecialistDomain.POD, force=force),
            self.check_specialist(SpecialistDomain.NETWORK, force=force),
            self.check_specialist(SpecialistDomain.INFRASTRUCTURE, force=force),
            self.check_specialist(SpecialistDomain.APPLICATION, force=force),
            self.check_coordinator(force=force),
            self.check_executor(force=force),
            self.check_embeddings(force=force),
            return_exceptions=True,
        )

        results: dict[str, AgentHealthResult] = {}
        names = [
            "triage", "specialist_pod", "specialist_network",
            "specialist_infrastructure", "specialist_application",
            "coordinator", "executor", "embeddings",
        ]
        for name, result in zip(names, checks):
            if isinstance(result, Exception):
                results[name] = AgentHealthResult(
                    agent_name=name, status="error",
                    error=f"{type(result).__name__}: {result}",
                )
            else:
                results[name] = result

        return results

    def _get_cached(self, key: str) -> AgentHealthResult | None:
        if key in self._cache:
            ts, result = self._cache[key]
            if time.time() - ts < HEALTH_CACHE_TTL:
                result.cached = True
                return result
        return None

    def _set_cache(self, key: str, result: AgentHealthResult) -> AgentHealthResult:
        result.cached = False
        self._cache[key] = (time.time(), result)
        return result

    # ------------------------------------------------------------------
    # Triage Agent (Haiku)
    # ------------------------------------------------------------------
    async def check_triage(self, force: bool = False) -> AgentHealthResult:
        """Check triage agent health: API key + model ping."""
        name = "triage"
        if not force:
            cached = self._get_cached(name)
            if cached:
                return cached

        if not settings.multi_agent_enabled:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="not_configured",
                details={"reason": "MULTI_AGENT_ENABLED=false"},
            ))

        if not settings.anthropic_api_key:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="error",
                error="ANTHROPIC_API_KEY not set",
            ))

        # Ping the model with a minimal request
        result = await self._ping_model(name, settings.triage_model)
        result.details["fallback"] = "deterministic routing (always available)"
        return self._set_cache(name, result)

    # ------------------------------------------------------------------
    # Specialist Agents (Sonnet)
    # ------------------------------------------------------------------
    async def check_specialist(
        self, domain: SpecialistDomain, force: bool = False
    ) -> AgentHealthResult:
        """Check specialist agent health: API key + model + tool registry."""
        name = f"specialist_{domain.value}"
        if not force:
            cached = self._get_cached(name)
            if cached:
                return cached

        if not settings.multi_agent_enabled:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="not_configured",
                details={"reason": "MULTI_AGENT_ENABLED=false"},
            ))

        if not settings.anthropic_api_key:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="error",
                error="ANTHROPIC_API_KEY not set",
            ))

        # Check tool registry
        expected_tools = get_domain_tool_names(domain)
        try:
            from ..tool_registry import build_domain_registry
            registry = build_domain_registry(expected_tools)
            actual_count = len(registry.tool_names)
            missing = set(expected_tools) - set(registry.tool_names)
        except Exception as e:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="error",
                model=settings.specialist_model,
                error=f"Tool registry build failed: {e}",
            ))

        # Ping the model
        result = await self._ping_model(name, settings.specialist_model)
        result.tool_count = actual_count
        result.details["domain"] = domain.value
        result.details["expected_tools"] = len(expected_tools)
        if missing:
            result.status = "degraded"
            result.details["missing_tools"] = list(missing)

        return self._set_cache(name, result)

    # ------------------------------------------------------------------
    # Coordinator Agent (Opus)
    # ------------------------------------------------------------------
    async def check_coordinator(self, force: bool = False) -> AgentHealthResult:
        """Check coordinator agent health: API key + model ping."""
        name = "coordinator"
        if not force:
            cached = self._get_cached(name)
            if cached:
                return cached

        if not settings.multi_agent_enabled:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="not_configured",
                details={"reason": "MULTI_AGENT_ENABLED=false"},
            ))

        if not settings.anthropic_api_key:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="error",
                error="ANTHROPIC_API_KEY not set",
            ))

        result = await self._ping_model(name, settings.coordinator_model)
        result.details["token_budget"] = settings.coordinator_token_budget
        result.details["activation"] = "correlated alerts only"
        return self._set_cache(name, result)

    # ------------------------------------------------------------------
    # Executor Agent (Sonnet — already exists)
    # ------------------------------------------------------------------
    async def check_executor(self, force: bool = False) -> AgentHealthResult:
        """Check executor agent health: API key + model + mutation tools."""
        name = "executor"
        if not force:
            cached = self._get_cached(name)
            if cached:
                return cached

        if not settings.anthropic_api_key:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="error",
                error="ANTHROPIC_API_KEY not set",
            ))

        # Verify mutation tools are available
        try:
            from ..tool_registry import build_default_registry
            from .tool_subsets import MUTATION_TOOLS

            registry = build_default_registry()
            available_mutations = [t for t in registry.tool_names if t in MUTATION_TOOLS]
        except Exception as e:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="error",
                error=f"Registry check failed: {e}",
            ))

        result = await self._ping_model(name, settings.specialist_model)
        result.tool_count = len(available_mutations)
        result.details["mutation_tools"] = available_mutations
        result.details["dry_run_default"] = settings.dry_run_default
        return self._set_cache(name, result)

    # ------------------------------------------------------------------
    # Embeddings (Voyage AI)
    # ------------------------------------------------------------------
    async def check_embeddings(self, force: bool = False) -> AgentHealthResult:
        """Check embedding provider health."""
        name = "embeddings"
        if not force:
            cached = self._get_cached(name)
            if cached:
                return cached

        if not settings.incident_memory_enabled:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="not_configured",
                details={"reason": "INCIDENT_MEMORY_ENABLED=false"},
            ))

        try:
            from ..embeddings import embedding_provider

            health = await embedding_provider.health_check()
            status = health.get("status", "error")
            return self._set_cache(name, AgentHealthResult(
                agent_name=name,
                status=status,
                model=health.get("model", ""),
                latency_ms=health.get("latency_ms", 0),
                details={
                    "dimensions": health.get("dimensions", 0),
                    "available": health.get("available", False),
                },
            ))
        except Exception as e:
            return self._set_cache(name, AgentHealthResult(
                agent_name=name, status="error",
                error=str(e),
            ))

    # ------------------------------------------------------------------
    # Model ping helper
    # ------------------------------------------------------------------
    async def _ping_model(self, agent_name: str, model: str) -> AgentHealthResult:
        """Ping a Claude model with a minimal 1-token request to verify reachability.

        This is intentionally cheap — just confirms auth + model access.
        Cached per-agent for 60s.
        """
        try:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            start = time.monotonic()
            response = await asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            latency = (time.monotonic() - start) * 1000

            return AgentHealthResult(
                agent_name=agent_name,
                status="ok",
                model=model,
                latency_ms=latency,
            )
        except anthropic.AuthenticationError:
            return AgentHealthResult(
                agent_name=agent_name, status="error",
                model=model, error="Authentication failed — invalid API key",
            )
        except anthropic.NotFoundError:
            return AgentHealthResult(
                agent_name=agent_name, status="error",
                model=model, error=f"Model '{model}' not found or not accessible",
            )
        except anthropic.RateLimitError:
            # Rate limited means the API IS reachable — just busy
            return AgentHealthResult(
                agent_name=agent_name, status="degraded",
                model=model,
                details={"reason": "Rate limited — API reachable but throttled"},
            )
        except anthropic.APIConnectionError as e:
            return AgentHealthResult(
                agent_name=agent_name, status="error",
                model=model, error=f"Cannot reach Anthropic API: {e}",
            )
        except Exception as e:
            return AgentHealthResult(
                agent_name=agent_name, status="error",
                model=model, error=f"{type(e).__name__}: {e}",
            )


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------
def summarize_health(results: dict[str, AgentHealthResult]) -> dict[str, Any]:
    """Produce a summary dict suitable for the /ready/agents endpoint."""
    agents = {name: r.to_dict() for name, r in results.items()}

    statuses = [r.status for r in results.values()]
    if all(s == "ok" for s in statuses):
        overall = "all_healthy"
    elif all(s in ("ok", "not_configured") for s in statuses):
        overall = "all_healthy"
    elif any(s == "error" for s in statuses):
        # Only count errors for configured agents
        configured_errors = [
            r for r in results.values()
            if r.status == "error" and "not_configured" not in str(r.details)
        ]
        if configured_errors:
            overall = "unhealthy"
        else:
            overall = "all_healthy"
    else:
        overall = "degraded"

    return {
        "status": overall,
        "multi_agent_enabled": settings.multi_agent_enabled,
        "agents": agents,
        "healthy_count": sum(1 for s in statuses if s == "ok"),
        "degraded_count": sum(1 for s in statuses if s == "degraded"),
        "error_count": sum(1 for s in statuses if s == "error"),
        "not_configured_count": sum(1 for s in statuses if s == "not_configured"),
    }


# Module-level singleton
agent_health_checker = AgentHealthChecker()
