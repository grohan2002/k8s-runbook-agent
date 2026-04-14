"""Fix Verification — secondary Claude review of diagnosis + fix proposals.

A cheap, fast reviewer (Haiku) checks the proposed fix against evidence
before posting to Slack. Catches evidence-diagnosis mismatches, symptom-only
fixes, missing rollback plans, and incorrect risk assessments.

Verdicts:
  APPROVE — fix looks sound, proceed to Slack
  REVISE  — feedback provided, original agent retries once
  REJECT  — fix should not be proposed, escalate instead

Fail-open: if the reviewer call fails, the fix proceeds anyway.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

import anthropic

from ..config import settings
from .session import DiagnosisSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
class VerificationVerdict(str, Enum):
    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"


@dataclass
class VerificationResult:
    verdict: VerificationVerdict
    feedback: str = ""
    raw_response: str = ""


# ---------------------------------------------------------------------------
# Reviewer prompt
# ---------------------------------------------------------------------------
REVIEWER_SYSTEM_PROMPT = """\
You are a Kubernetes fix reviewer. A diagnostic agent investigated a K8s alert \
and proposed a fix. Your job is to review the proposal for quality and safety.

Check ALL 8 criteria:

1. EVIDENCE → ROOT CAUSE: Does the evidence actually support the stated root cause? \
   Look for logical gaps — e.g., "exit code 1" does not prove OOMKilled.
2. ROOT CAUSE vs SYMPTOM: Does the fix address the root cause, not just a symptom? \
   "Restart pod" is a symptom fix if the root cause is a config error. \
   "Increase memory" is a symptom fix if the root cause is a memory leak. \
   REVISE if the fix is treating a symptom.
3. RISK ACCURACY: Is the risk level correct? Consider deployment strategy \
   (Recreate = full downtime = HIGH risk), stateful vs stateless, and blast radius.
4. ROLLBACK PLAN: Is it complete, specific, and executable? "Undo the change" is too vague.
5. SIDE EFFECTS: Could this fix break other workloads? Check for HPA interactions, \
   shared ConfigMaps, node resource pressure, or cross-service dependencies.
6. PREREQUISITES: Does the fix assume resources exist (ConfigMaps, Secrets, Services) \
   that the evidence didn't verify? If yes, REVISE.
7. COMPOUND FIX: Does the fix require multiple sequential steps? If step 2 depends on \
   step 1, the executor may only do step 2. REVISE with "split into sequential fixes."
8. FIX MATCHES TOOLS: Can the fix be executed with K8s patch/scale/restart/delete tools? \
   If it requires manual actions or tools the agent doesn't have, mark HUMAN_VALUES_NEEDED.

Output EXACTLY one of:

APPROVE
REVISE: <specific feedback on what needs to change>
REJECT: <reason the fix should not be proposed>

Be concise. One paragraph max.
"""


# ---------------------------------------------------------------------------
# Main verification function
# ---------------------------------------------------------------------------
async def verify_fix(
    session: DiagnosisSession,
    tool_results_summary: str,
) -> VerificationResult:
    """Run a single Haiku call to review the proposed fix.

    Returns VerificationResult. Never raises — returns APPROVE on any error (fail-open).
    """
    model = settings.fix_verification_model
    max_tokens = settings.fix_verification_max_tokens

    user_message = _build_reviewer_message(session, tool_results_summary)

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = await asyncio.to_thread(
            client.messages.create,
            model=model,
            max_tokens=max_tokens,
            system=REVIEWER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        result = _parse_verdict(text)
        logger.info(
            "Verification for %s: verdict=%s (model=%s, tokens=%d+%d)",
            session.id, result.verdict.value, model,
            response.usage.input_tokens, response.usage.output_tokens,
        )
        return result

    except Exception as e:
        logger.warning("Verification call failed for %s: %s (proceeding anyway)", session.id, e)
        return VerificationResult(
            verdict=VerificationVerdict.APPROVE,
            feedback="Verification skipped due to API error",
            raw_response=str(e),
        )


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------
def _build_reviewer_message(session: DiagnosisSession, tool_results_summary: str) -> str:
    """Build the reviewer's input from session state."""
    parts: list[str] = []

    # Alert context
    parts.append(f"## Alert\n- Name: {session.alert.alert_name}")
    parts.append(f"- Namespace: {session.alert.namespace}")
    if session.alert.pod:
        parts.append(f"- Pod: {session.alert.pod}")
    parts.append(f"- Severity: {session.alert.severity}")

    # Diagnosis
    if session.diagnosis:
        d = session.diagnosis
        parts.append(f"\n## Diagnosis")
        parts.append(f"- Root Cause: {d.root_cause}")
        parts.append(f"- Confidence: {d.confidence.value}")
        if d.evidence:
            parts.append("- Evidence:")
            for e in d.evidence:  # Full list — no truncation
                parts.append(f"  - {e}")
        if d.ruled_out:
            parts.append("- Ruled Out:")
            for r in d.ruled_out:  # Full list
                parts.append(f"  - {r}")

    # Fix proposal — full content for accurate review
    if session.fix_proposal:
        f = session.fix_proposal
        parts.append(f"\n## Proposed Fix")
        parts.append(f"- Summary: {f.summary}")
        parts.append(f"- Risk: {f.risk_level.value}")
        parts.append(f"- Description: {f.description}")  # Full — no truncation
        if f.dry_run_output:
            parts.append(f"- Dry Run: {f.dry_run_output}")  # Full
        parts.append(f"- Rollback Plan: {f.rollback_plan or 'NONE PROVIDED'}")
        if f.requires_human_values:
            parts.append(f"- Needs Human Input: {', '.join(f.human_value_fields)}")

    # Raw evidence (tool results)
    if tool_results_summary:
        parts.append(f"\n## Raw Evidence (tool call results)\n{tool_results_summary[:3000]}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Verdict parser
# ---------------------------------------------------------------------------
def _parse_verdict(text: str) -> VerificationResult:
    """Parse the reviewer's output into a structured verdict.

    Fail-open: returns APPROVE if the output can't be parsed.
    """
    text = text.strip()

    # Check for REJECT first (most specific)
    m = re.match(r"REJECT[:\s]*(.+)", text, re.DOTALL | re.IGNORECASE)
    if m:
        return VerificationResult(
            verdict=VerificationVerdict.REJECT,
            feedback=m.group(1).strip(),
            raw_response=text,
        )

    # Check for REVISE
    m = re.match(r"REVISE[:\s]*(.+)", text, re.DOTALL | re.IGNORECASE)
    if m:
        return VerificationResult(
            verdict=VerificationVerdict.REVISE,
            feedback=m.group(1).strip(),
            raw_response=text,
        )

    # Check for APPROVE (with optional trailing text)
    if text.upper().startswith("APPROVE"):
        return VerificationResult(
            verdict=VerificationVerdict.APPROVE,
            raw_response=text,
        )

    # Fallback: can't parse → fail-open (APPROVE)
    logger.warning("Could not parse verification verdict: %s", text[:200])
    return VerificationResult(
        verdict=VerificationVerdict.APPROVE,
        feedback="Verification output unparseable — proceeding",
        raw_response=text,
    )


# ---------------------------------------------------------------------------
# Tool results extractor
# ---------------------------------------------------------------------------
def extract_tool_results_summary(session: DiagnosisSession) -> str:
    """Extract a summary of tool call results from the session conversation.

    Scans session.messages for tool_result content blocks.
    Truncates to ~4000 chars total to fit in the reviewer's context.
    """
    summaries: list[str] = []
    total_chars = 0
    max_chars = 4000

    # Track tool names from assistant messages
    tool_names: dict[str, str] = {}  # tool_use_id → tool_name
    for msg in session.messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_names[block["id"]] = block["name"]

    # Extract tool results
    for msg in session.messages:
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id", "")
                        name = tool_names.get(tool_id, "unknown_tool")
                        result_text = block.get("content", "")
                        if isinstance(result_text, str):
                            # Truncate individual results
                            truncated = result_text[:800]
                            entry = f"### {name}\n{truncated}"
                            if total_chars + len(entry) > max_chars:
                                break
                            summaries.append(entry)
                            total_chars += len(entry)

    return "\n\n".join(summaries) if summaries else "(no tool results available)"
