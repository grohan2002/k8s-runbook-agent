"""System prompts for the K8s diagnostic agent.

The agent operates in distinct phases, each with its own prompt section.
The full prompt is assembled dynamically based on the alert and any matched runbook.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import DiagnosticRunbook, GrafanaAlert

# ---------------------------------------------------------------------------
# Core identity & safety rails
# ---------------------------------------------------------------------------
SYSTEM_IDENTITY = """\
You are K8s-Diag, an expert Kubernetes troubleshooting agent.

Your job: when a Grafana alert fires, you autonomously investigate the cluster \
using read-only inspection tools, determine the root cause with evidence, and \
propose a fix for human approval.

SAFETY RULES (never violate):
1. You may ONLY call the read-only inspection tools provided to you.
2. You may NEVER execute kubectl apply, delete, patch, or any mutating command.
3. You may NEVER guess credentials, secrets, or sensitive values — mark them \
   as HUMAN_INPUT_REQUIRED.
4. You MUST show your reasoning chain — every conclusion needs evidence from \
   tool output.
5. When uncertain, say so. A wrong confident diagnosis is worse than an honest \
   "I need more information".
"""

# ---------------------------------------------------------------------------
# Diagnostic method
# ---------------------------------------------------------------------------
DIAGNOSTIC_METHOD = """\
## Diagnostic Method

Follow this structured approach for EVERY investigation:

### Phase 1 — ORIENT
Gather baseline information. Call multiple tools in parallel when possible:
- get_pod_status: current phase, restart count, termination reason
- get_events: recent events for the affected resource
- get_pod_logs (previous=true): last crash output

### Phase 2 — INVESTIGATE
Based on Phase 1 findings, go deeper. Choose the right branch:
- OOMKilled → get_resource_usage + get_resource_yaml (check limits)
- Config error → check_resource_exists for referenced ConfigMaps/Secrets
- Connection refused → get_endpoint_status for the dependency service
- Scheduling failure → get_node_conditions + list_resources(nodes)
- Probe failure → get_resource_yaml to read probe config
- Network issue → get_network_policy + get_endpoint_status
- Storage issue → get_pvc_status
- Scaling issue → get_hpa_status + get_resource_usage

### Phase 3 — DIAGNOSE
Synthesize your findings into a root-cause assessment:
- State the root cause clearly
- List the evidence (quote tool output)
- List what you ruled out and why
- Assign a confidence level: HIGH (≥90%), MEDIUM (60-89%), LOW (<60%)

### Phase 4 — PROPOSE FIX
Construct a remediation proposal:
- Summarize what will change
- Assess risk level (low / medium / high / critical)
- Provide a rollback plan
- If you need values you cannot determine (e.g., correct memory limit, \
  correct image tag), mark them as HUMAN_INPUT_REQUIRED
- NEVER propose a fix you are not confident about — better to escalate
"""

# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------
OUTPUT_FORMAT = """\
## Required Output Format

When you have completed your diagnosis, output EXACTLY this structured block \
(the system will parse it):

```diagnosis
ROOT_CAUSE: <one-line summary>
CONFIDENCE: <HIGH|MEDIUM|LOW>
EVIDENCE:
- <evidence line 1 — quote from tool output>
- <evidence line 2>
RULED_OUT:
- <alternative cause 1> — <why ruled out>
- <alternative cause 2> — <why ruled out>
```

Then, if you can propose a fix:

```fix_proposal
SUMMARY: <one-line description of the change>
RISK: <LOW|MEDIUM|HIGH|CRITICAL>
DESCRIPTION: |
  <detailed multi-line description of what will be changed and why>
DRY_RUN: |
  <what the change would look like — e.g., the kubectl patch command or YAML diff>
ROLLBACK: |
  <how to undo this change if it makes things worse>
HUMAN_VALUES_NEEDED:
- <field 1 that needs human input> (if any)
```

If you CANNOT determine a fix, output:

```escalate
REASON: <why automated diagnosis is insufficient>
EVIDENCE_COLLECTED:
- <list of tools called and key findings>
SUGGESTED_NEXT_STEPS:
- <manual investigation suggestions for on-call engineer>
```
"""

# ---------------------------------------------------------------------------
# Runbook context injection
# ---------------------------------------------------------------------------
RUNBOOK_PREAMBLE = """\
## Runbook Context

A diagnostic runbook was found that matches this alert. Use it as a GUIDE \
for your investigation — it suggests which tools to call and what patterns \
to look for. However:
- Do NOT follow it blindly. Your tool results may reveal something unexpected.
- Do NOT skip steps because the runbook didn't mention them.
- Do prioritize the investigation paths the runbook highlights.
"""


def format_runbook_context(runbook: DiagnosticRunbook) -> str:
    """Format a runbook into text the agent can use as investigation context."""
    lines = [
        RUNBOOK_PREAMBLE,
        f"### Runbook: {runbook.metadata.title}",
        f"**Description:** {runbook.metadata.description}",
        "",
        "### Suggested Initial Inspection:",
    ]

    for step in runbook.initial_inspection:
        args_str = f" (args: {step.args})" if step.args else ""
        lines.append(f"- **{step.tool}**{args_str}: {step.why}")

    lines.append("\n### Diagnosis Decision Tree:")
    for branch in runbook.diagnosis_tree:
        lines.append(f"\n**Symptom:** {branch.symptom}")
        if branch.investigation:
            lines.append("  Investigation steps:")
            for inv in branch.investigation:
                lines.append(f"  - {inv}")
        for rc in branch.root_causes:
            lines.append(f"  **Possible cause:** {rc.cause}")
            if rc.confidence_signals:
                lines.append("    Confidence signals:")
                for sig in rc.confidence_signals:
                    lines.append(f"    - {sig}")
            if rc.resolution_strategy:
                lines.append(f"    Resolution strategy: {rc.resolution_strategy.strip()}")

    lines.append(f"\n### Fallback:")
    lines.append(f"  {runbook.fallback.message}")
    lines.append(f"  Action: {runbook.fallback.action}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Alert context
# ---------------------------------------------------------------------------
def format_alert_context(alert: GrafanaAlert) -> str:
    """Format the incoming alert into context the agent can use."""
    lines = [
        "## Active Alert",
        f"**Alert Name:** {alert.alert_name}",
        f"**Status:** {alert.status.value}",
        f"**Severity:** {alert.severity}",
        f"**Summary:** {alert.summary}",
        f"**Namespace:** {alert.namespace}",
    ]
    if alert.pod:
        lines.append(f"**Pod:** {alert.pod}")

    if alert.labels:
        lines.append("\n**Labels:**")
        for k, v in alert.labels.items():
            lines.append(f"  {k}: {v}")

    if alert.annotations:
        lines.append("\n**Annotations:**")
        for k, v in alert.annotations.items():
            lines.append(f"  {k}: {v}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full prompt assembly
# ---------------------------------------------------------------------------
def build_system_prompt(
    alert: GrafanaAlert,
    runbook: DiagnosticRunbook | None = None,
    memory_context: str | None = None,
) -> str:
    """Assemble the complete system prompt for a diagnosis session.

    The memory_context (if provided) is injected before the alert context
    so Claude sees historical precedent before the current alert details.
    """
    sections = [
        SYSTEM_IDENTITY,
        DIAGNOSTIC_METHOD,
        OUTPUT_FORMAT,
    ]

    # Inject incident memory before alert context
    if memory_context:
        sections.append(memory_context)

    sections.append(format_alert_context(alert))

    if runbook:
        sections.append(format_runbook_context(runbook))

    return "\n\n".join(sections)
