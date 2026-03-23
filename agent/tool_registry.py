"""Tool registry — converts our tool functions into Anthropic API tool definitions.

The Anthropic Messages API expects tools in this shape:
{
    "name": "get_pod_status",
    "description": "...",
    "input_schema": { "type": "object", "properties": {...}, "required": [...] }
}

This module builds that list from our tool metadata, and provides a dispatcher
that routes tool_use blocks back to the correct async function.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definition dataclass
# ---------------------------------------------------------------------------
class ToolDef:
    """One registered tool with its API schema and handler function."""

    __slots__ = ("name", "description", "parameters", "handler")

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, type],
        handler: Callable[..., Coroutine],
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    def to_anthropic_schema(self) -> dict[str, Any]:
        """Convert to the dict format expected by Anthropic's Messages API."""
        properties: dict[str, Any] = {}
        for param_name, param_type in self.parameters.items():
            if param_type is str:
                properties[param_name] = {"type": "string"}
            elif param_type is int:
                properties[param_name] = {"type": "integer"}
            elif param_type is float:
                properties[param_name] = {"type": "number"}
            elif param_type is bool:
                properties[param_name] = {"type": "boolean"}
            else:
                properties[param_name] = {"type": "string"}

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                # All params are optional — tools handle defaults internally
                "required": [],
            },
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class ToolRegistry:
    """Central registry of all tools available to the agent."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, type],
        handler: Callable[..., Coroutine],
    ) -> None:
        self._tools[name] = ToolDef(name, description, parameters, handler)
        logger.debug("Registered tool: %s", name)

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def to_anthropic_tools(self) -> list[dict[str, Any]]:
        """Return the full tool list for the Anthropic Messages API."""
        return [t.to_anthropic_schema() for t in self._tools.values()]

    async def dispatch(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool by name and return its result.

        Returns a dict with 'content' (list of content blocks) and optionally 'is_error'.
        """
        tool_def = self._tools.get(tool_name)
        if not tool_def:
            return {
                "content": [{"type": "text", "text": f"ERROR: Unknown tool '{tool_name}'"}],
                "is_error": True,
            }

        try:
            result = await tool_def.handler(tool_input)
            return result
        except Exception as e:
            logger.exception("Tool %s failed with error", tool_name)
            return {
                "content": [{"type": "text", "text": f"ERROR: Tool '{tool_name}' failed: {e}"}],
                "is_error": True,
            }


# ---------------------------------------------------------------------------
# Build the default registry from our tool modules
# ---------------------------------------------------------------------------
def build_default_registry() -> ToolRegistry:
    """Import all tool modules and register their @tool-decorated functions.

    Our tools use a simple @tool decorator pattern:
        @tool("name", "description", {"param": type})
        async def handler(args: dict) -> dict: ...

    We extract the metadata and register each one.
    """
    registry = ToolRegistry()

    # Import tool modules — this triggers module-level setup
    from ..tools import cluster_inspect, cluster_mutate, knowledge_base  # noqa: F401

    # Collect all tools from each module
    _register_module_tools(registry, cluster_inspect)
    _register_module_tools(registry, knowledge_base)
    _register_module_tools(registry, cluster_mutate)

    logger.info("Tool registry built: %d tools registered", len(registry.tool_names))
    return registry


def build_domain_registry(tool_names: list[str]) -> ToolRegistry:
    """Build a filtered registry containing only the specified tools.

    Used by specialist agents to get domain-specific tool subsets.
    Loads the full registry once, then cherry-picks the requested tools.
    """
    full = build_default_registry()
    filtered = ToolRegistry()

    for name in tool_names:
        tool_def = full.get(name)
        if tool_def:
            filtered.register(
                name=tool_def.name,
                description=tool_def.description,
                parameters=tool_def.parameters,
                handler=tool_def.handler,
            )
        else:
            logger.warning("build_domain_registry: tool '%s' not found in full registry", name)

    logger.info("Domain registry built: %d/%d tools", len(filtered.tool_names), len(tool_names))
    return filtered


def _register_module_tools(registry: ToolRegistry, module: Any) -> None:
    """Scan a module for functions decorated with @tool and register them."""
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        # Our @tool decorator attaches metadata attributes
        if callable(obj) and hasattr(obj, "_tool_name"):
            registry.register(
                name=obj._tool_name,
                description=obj._tool_description,
                parameters=obj._tool_parameters,
                handler=obj,
            )
