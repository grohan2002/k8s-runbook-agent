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


# ---------------------------------------------------------------------------
# Required tools per domain — minimum set that MUST be called before diagnosis
# ---------------------------------------------------------------------------
REQUIRED_TOOLS: dict[SpecialistDomain, frozenset[str]] = {
    SpecialistDomain.POD: frozenset({"get_pod_status", "get_pod_logs", "get_events"}),
    SpecialistDomain.NETWORK: frozenset({"get_endpoint_status", "get_events", "describe_resource"}),
    SpecialistDomain.INFRASTRUCTURE: frozenset({"get_node_conditions", "get_events", "get_resource_usage"}),
    SpecialistDomain.APPLICATION: frozenset({"get_pod_status", "get_pod_logs", "get_endpoint_status"}),
}

# For single-agent (general) mode: require get_events + at least one of these
GENERAL_REQUIRED_TOOLS: frozenset[str] = frozenset({"get_events"})
GENERAL_REQUIRED_EITHER: list[frozenset[str]] = [frozenset({"get_pod_status", "get_node_conditions"})]

MAX_ENFORCEMENT_ROUNDS = 3


def check_required_tools_met(
    domain: SpecialistDomain | None,
    tools_called: set[str],
) -> tuple[bool, set[str]]:
    """Check if required tools for a domain have all been called.

    Returns (met, missing_tools).
    For general mode (domain=None), requires GENERAL_REQUIRED_TOOLS
    plus at least one tool from each set in GENERAL_REQUIRED_EITHER.
    """
    if domain is not None:
        required = REQUIRED_TOOLS.get(domain, frozenset())
        missing = required - tools_called
        return (len(missing) == 0, missing)

    # General (single-agent) mode
    missing: set[str] = set(GENERAL_REQUIRED_TOOLS - tools_called)
    for either_set in GENERAL_REQUIRED_EITHER:
        if not (either_set & tools_called):
            missing.add(f"one of {{{', '.join(sorted(either_set))}}}")
    return (len(missing) == 0, missing)


def get_domain_tool_names(domain: SpecialistDomain) -> list[str]:
    """Get the tool names for a specialist domain."""
    return DOMAIN_TOOLS[domain]


def validate_no_mutations(tool_names: list[str]) -> None:
    """Assert that no mutation tools are in a specialist's tool set."""
    leaked = MUTATION_TOOLS & set(tool_names)
    if leaked:
        raise ValueError(f"Mutation tools leaked into specialist tools: {leaked}")
