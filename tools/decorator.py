"""Lightweight @tool decorator that attaches metadata for the tool registry.

Replaces the `claude_agent_sdk.tool` import — our tools use this decorator
to declare their name, description, and parameter types. The tool_registry
module scans for these attributes to build the Anthropic API tool list.

Usage:
    @tool("get_pod_status", "Get pod status...", {"namespace": str, "pod_name": str})
    async def get_pod_status(args: dict) -> dict:
        ...
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine


def tool(
    name: str,
    description: str,
    parameters: dict[str, type],
) -> Callable:
    """Decorator that attaches MCP tool metadata to an async function."""

    def decorator(
        func: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
    ) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
        func._tool_name = name  # type: ignore[attr-defined]
        func._tool_description = description  # type: ignore[attr-defined]
        func._tool_parameters = parameters  # type: ignore[attr-defined]
        return func

    return decorator
