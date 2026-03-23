"""MCP tool for searching and retrieving diagnostic runbooks."""

from __future__ import annotations

from typing import Any

import yaml
from .decorator import tool

from ..config import settings
from ..knowledge.loader import RunbookStore

# Module-level store — loaded once at import time
_store = RunbookStore()
_store.load_directory(settings.runbook_dir)


def _runbook_to_text(rb) -> str:
    """Serialize a DiagnosticRunbook to readable text for the agent."""
    lines = [
        f"# Runbook: {rb.metadata.title}",
        f"ID: {rb.metadata.id}",
        f"Description: {rb.metadata.description}",
        f"Tags: {', '.join(rb.metadata.tags)}",
        "",
        "## Initial Inspection Steps",
    ]
    for i, step in enumerate(rb.initial_inspection, 1):
        lines.append(f"  {i}. Tool: {step.tool}")
        lines.append(f"     Why: {step.why}")
        if step.args:
            lines.append(f"     Args: {step.args}")

    lines.append("")
    lines.append("## Diagnosis Tree")
    for branch in rb.diagnosis_tree:
        lines.append(f"\n### Symptom: {branch.symptom}")
        if branch.investigation:
            lines.append("  Investigation:")
            for inv in branch.investigation:
                lines.append(f"    - {inv}")
        for rc in branch.root_causes:
            lines.append(f"\n  Root Cause: {rc.cause}")
            if rc.confidence_signals:
                lines.append("  Confidence Signals:")
                for sig in rc.confidence_signals:
                    lines.append(f"    - {sig}")
            if rc.resolution_strategy:
                lines.append(f"  Resolution Strategy:\n    {rc.resolution_strategy.strip()}")

    lines.append("")
    lines.append("## Fallback")
    lines.append(f"  Message: {rb.fallback.message}")
    lines.append(f"  Action: {rb.fallback.action}")
    if rb.fallback.collect:
        lines.append("  Collect:")
        for item in rb.fallback.collect:
            lines.append(f"    - {item}")

    return "\n".join(lines)


@tool(
    "search_runbooks",
    "Search the diagnostic runbook knowledge base for runbooks matching an alert. "
    "Returns matched runbooks ranked by relevance. Pass the alert name from Grafana, "
    "optional free-text query describing symptoms, and any relevant labels.",
    {
        "alert_name": str,
        "query": str,
        "labels": str,
    },
)
async def search_runbooks(args: dict[str, Any]) -> dict[str, Any]:
    alert_name = args.get("alert_name", "")
    query = args.get("query", "")
    labels_str = args.get("labels", "")

    # Parse labels from "key=value,key2=value2" format
    labels: dict[str, str] = {}
    if labels_str:
        for pair in labels_str.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                labels[k.strip()] = v.strip()

    matches = _store.search(query=query, alert_name=alert_name, labels=labels)

    if not matches:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"No runbooks matched alert_name='{alert_name}', query='{query}'.\n"
                        "You should diagnose this alert using your general Kubernetes expertise.\n"
                        "Use the cluster inspection tools to gather evidence and reason from first principles."
                    ),
                }
            ]
        }

    # Return full runbook content for the top match, summaries for the rest
    lines = [f"Found {len(matches)} matching runbook(s):\n"]

    for i, match in enumerate(matches):
        rb = _store.get(match.runbook_id)
        if i == 0 and rb:
            # Full content for the best match
            lines.append(f"--- BEST MATCH (score: {match.score}) ---")
            lines.append(f"Match reasons: {', '.join(match.match_reasons)}")
            lines.append("")
            lines.append(_runbook_to_text(rb))
        else:
            # Summary for other matches
            lines.append(f"\n--- Other match (score: {match.score}) ---")
            lines.append(f"ID: {match.runbook_id}")
            lines.append(f"Title: {match.title}")
            lines.append(f"Match reasons: {', '.join(match.match_reasons)}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "get_runbook",
    "Retrieve the full diagnostic runbook by its ID. Use this when you want to "
    "read a specific runbook in detail after seeing it in search results.",
    {"runbook_id": str},
)
async def get_runbook(args: dict[str, Any]) -> dict[str, Any]:
    runbook_id = args["runbook_id"]
    rb = _store.get(runbook_id)

    if not rb:
        available = [r.metadata.id for r in _store.all_runbooks]
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Runbook '{runbook_id}' not found. Available: {', '.join(available)}",
                }
            ]
        }

    return {"content": [{"type": "text", "text": _runbook_to_text(rb)}]}


def get_store() -> RunbookStore:
    """Access the module-level RunbookStore (for testing or direct use)."""
    return _store
