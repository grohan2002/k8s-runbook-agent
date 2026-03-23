"""Domain-specific tool subsets for specialist agents.

Each specialist gets only the K8s inspection tools relevant to its domain.
No specialist receives mutation tools — those stay with the Executor.
"""

from __future__ import annotations

from enum import Enum


class SpecialistDomain(str, Enum):
    POD = "pod"
    NETWORK = "network"
    INFRASTRUCTURE = "infrastructure"
    APPLICATION = "application"


# Shared across all specialists
_COMMON_TOOLS = [
    "get_events",
    "describe_resource",
    "get_resource_yaml",
    "list_resources",
    "search_runbooks",
    "get_runbook",
]

DOMAIN_TOOLS: dict[SpecialistDomain, list[str]] = {
    SpecialistDomain.POD: [
        "get_pod_status",
        "get_pod_logs",
        "get_resource_usage",
        "check_resource_exists",
        "get_node_conditions",
        *_COMMON_TOOLS,
    ],
    SpecialistDomain.NETWORK: [
        "get_endpoint_status",
        "get_ingress_status",
        "get_network_policy",
        "get_pod_status",
        "check_resource_exists",
        *_COMMON_TOOLS,
    ],
    SpecialistDomain.INFRASTRUCTURE: [
        "get_node_conditions",
        "get_hpa_status",
        "get_pvc_status",
        "get_resource_usage",
        "get_pod_status",
        *_COMMON_TOOLS,
    ],
    SpecialistDomain.APPLICATION: [
        "get_pod_status",
        "get_pod_logs",
        "get_resource_usage",
        "get_endpoint_status",
        "get_hpa_status",
        "get_ingress_status",
        "check_resource_exists",
        *_COMMON_TOOLS,
    ],
}

# Mutation tools — never given to specialists, only to Executor
MUTATION_TOOLS = frozenset({
    "patch_resource",
    "scale_deployment",
    "rollback_deployment",
    "restart_deployment",
    "delete_pod",
    "create_resource",
})


def get_domain_tool_names(domain: SpecialistDomain) -> list[str]:
    """Get the tool names for a specialist domain."""
    return DOMAIN_TOOLS[domain]


def validate_no_mutations(tool_names: list[str]) -> None:
    """Assert that no mutation tools are in a specialist's tool set."""
    leaked = MUTATION_TOOLS & set(tool_names)
    if leaked:
        raise ValueError(f"Mutation tools leaked into specialist tools: {leaked}")
