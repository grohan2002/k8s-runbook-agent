"""Fix execution engine.

Orchestrates the safe execution of an approved fix:
  1. Run guardrails → block if any fail
  2. Capture pre-state snapshot
  3. Execute dry-run → verify no errors
  4. Execute live mutation
  5. Capture post-state snapshot
  6. Verify fix (re-check pod status / events)
  7. If verification fails → auto-rollback

The executor uses Claude to translate the fix proposal's description into
the specific mutation tool calls needed, rather than executing raw commands.
This keeps the human-readable fix proposal decoupled from the tool API.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from ..config import settings
from ..models import RiskLevel
from .guardrails import GuardrailResult, evaluate_guardrails
from .session import DiagnosisSession, SessionPhase
from .tool_registry import ToolRegistry, build_default_registry

logger = logging.getLogger(__name__)

EXECUTOR_MODEL = "claude-sonnet-4-20250514"
MAX_EXECUTION_ROUNDS = 10


# ---------------------------------------------------------------------------
# Executor system prompt
# ---------------------------------------------------------------------------
EXECUTOR_SYSTEM_PROMPT = """\
You are a Kubernetes fix executor. A human has approved a specific fix proposal.
Your job is to execute it using the mutation tools available to you.

RULES:
1. Execute ONLY what the approved fix describes — nothing more.
2. Always run with dry_run=true FIRST. Only proceed to dry_run=false if the \
   dry run succeeds.
3. After executing the live change, verify the fix by checking pod status / events.
4. If something goes wrong, stop immediately and report the error. Do NOT retry.
5. You have BOTH read-only inspection tools AND mutation tools available.

## Fix to Execute
{fix_description}

## Pre-State Snapshot
{pre_state}

## Execution Steps
1. Run the mutation with dry_run=true
2. Verify the dry-run output looks correct
3. Run the mutation with dry_run=false (LIVE)
4. Wait a moment, then verify the change took effect using inspection tools
5. Report the result

Output your final result as:

```execution_result
STATUS: <SUCCESS|FAILED|ROLLBACK_NEEDED>
SUMMARY: <one-line summary of what happened>
DETAILS: |
  <multi-line details of the execution>
VERIFICATION: |
  <what you checked to verify the fix worked>
```
"""


# ---------------------------------------------------------------------------
# Pre-state snapshot capture
# ---------------------------------------------------------------------------
async def capture_pre_state(session: DiagnosisSession, registry: ToolRegistry) -> dict[str, Any]:
    """Capture the current state of affected resources before applying the fix."""
    alert = session.alert
    snapshot: dict[str, Any] = {"namespace": alert.namespace}

    # Capture pod status if we have a pod name
    if alert.pod:
        result = await registry.dispatch("get_pod_status", {
            "namespace": alert.namespace,
            "pod_name": alert.pod,
        })
        snapshot["pod_status"] = _extract_text(result)

    # Capture events
    result = await registry.dispatch("get_events", {
        "namespace": alert.namespace,
        "since_minutes": "10",
    })
    snapshot["events"] = _extract_text(result)

    # Try to get deployment name from pod labels or alert labels
    deployment = alert.labels.get("deployment") or alert.labels.get("app")
    if deployment:
        result = await registry.dispatch("describe_resource", {
            "kind": "deployment",
            "namespace": alert.namespace,
            "name": deployment,
        })
        snapshot["deployment"] = _extract_text(result)

    return snapshot


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------
class FixExecutor:
    """Executes an approved fix using Claude + mutation tools."""

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.registry = registry or build_default_registry()

    async def execute(self, session: DiagnosisSession) -> ExecutionResult:
        """Execute the approved fix for a session.

        Returns an ExecutionResult with success/failure status and details.
        """
        # 1. Guardrails
        guardrail_result = evaluate_guardrails(session)
        if not guardrail_result.passed:
            session.fail(f"Guardrails blocked execution: {guardrail_result.summary()}")
            return ExecutionResult(
                success=False,
                summary="Blocked by guardrails",
                details=guardrail_result.summary(),
                guardrail_result=guardrail_result,
            )

        logger.info("Session %s: guardrails passed, starting execution", session.id)

        # 2. Freshness check — re-inspect cluster state before executing
        stale_warning = await self._check_freshness(session)
        if stale_warning:
            logger.warning("Session %s: freshness check: %s", session.id, stale_warning)

        # 3. Capture pre-state
        try:
            pre_state = await capture_pre_state(session, self.registry)
            session.approval.pre_state_snapshot = pre_state
            if stale_warning:
                pre_state["freshness_warning"] = stale_warning
        except Exception as e:
            logger.exception("Failed to capture pre-state for session %s", session.id)
            pre_state = {"error": str(e)}

        # 4. Build executor prompt
        fix = session.fix_proposal
        system_prompt = EXECUTOR_SYSTEM_PROMPT.format(
            fix_description=self._format_fix_for_executor(session),
            pre_state=json.dumps(pre_state, indent=2, default=str)[:3000],
        )

        # 4. Run executor loop
        try:
            result_text = await self._run_executor_loop(session, system_prompt)
            execution_result = self._parse_execution_result(result_text)

            # 5. Capture post-state for before/after comparison
            try:
                post_state = await capture_pre_state(session, self.registry)
                execution_result.pre_state = pre_state
                execution_result.post_state = post_state
            except Exception:
                logger.warning("Failed to capture post-state for session %s", session.id)

            # 6. Record metrics and update session
            from ..observability.metrics import fixes_executed

            risk = session.fix_proposal.risk_level.value if session.fix_proposal else "unknown"
            if execution_result.success:
                session.mark_resolved(execution_result.summary)
                fixes_executed.inc({"result": "success", "risk_level": risk})
            else:
                session.fail(execution_result.summary)
                fixes_executed.inc({"result": "failed", "risk_level": risk})

            execution_result.guardrail_result = guardrail_result
            return execution_result

        except Exception as e:
            logger.exception("Executor failed for session %s", session.id)
            session.fail(f"Executor error: {e}")
            return ExecutionResult(
                success=False,
                summary=f"Executor error: {e}",
                details=str(e),
                guardrail_result=guardrail_result,
            )

    async def _check_freshness(self, session: DiagnosisSession) -> str | None:
        """Re-check cluster state before executing. Returns warning if stale, None if fresh."""
        alert = session.alert
        if not alert.pod:
            return None

        try:
            result = await self.registry.dispatch("get_pod_status", {
                "namespace": alert.namespace,
                "pod_name": alert.pod,
            })
            current_status = _extract_text(result)

            # Check if pod has self-healed
            if result.get("is_error"):
                if "not found" in current_status.lower():
                    return f"Pod {alert.pod} no longer exists — it may have been deleted or replaced."

            # Check for signs the issue resolved itself
            lower = current_status.lower()
            if "phase: running" in lower and "restarts: 0" in lower:
                return (
                    f"Pod {alert.pod} is now Running with 0 restarts. "
                    "The issue may have self-healed since diagnosis."
                )

            # Check if diagnosis mentioned OOMKilled but pod is now in different state
            if session.diagnosis and "oomkilled" in session.diagnosis.root_cause.lower():
                if "oomkilled" not in lower and "phase: running" in lower:
                    return (
                        "Diagnosis was OOMKilled but pod is now Running without OOM. "
                        "Condition may have changed."
                    )

        except Exception:
            logger.debug("Freshness check failed for %s — proceeding anyway", session.id)

        return None

    def _format_fix_for_executor(self, session: DiagnosisSession) -> str:
        """Format the approved fix proposal for the executor prompt."""
        fix = session.fix_proposal
        alert = session.alert
        lines = [
            f"Alert: {alert.alert_name}",
            f"Namespace: {alert.namespace}",
        ]
        if alert.pod:
            lines.append(f"Pod: {alert.pod}")

        lines.extend([
            f"\nFix Summary: {fix.summary}",
            f"Risk Level: {fix.risk_level.value}",
            f"\nDescription:\n{fix.description}",
        ])

        if fix.dry_run_output:
            lines.append(f"\nExpected Change (from diagnosis dry-run):\n{fix.dry_run_output}")

        if fix.rollback_plan:
            lines.append(f"\nRollback Plan:\n{fix.rollback_plan}")

        return "\n".join(lines)

    async def _run_executor_loop(self, session: DiagnosisSession, system_prompt: str) -> str:
        """Run the executor's tool-use loop. Returns the final text output."""

        tools = self.registry.to_anthropic_tools()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Execute the approved fix now. Start with a dry run."},
        ]

        full_text = ""
        rounds = 0

        while rounds < MAX_EXECUTION_ROUNDS:
            rounds += 1
            logger.info("Session %s executor: round %d/%d", session.id, rounds, MAX_EXECUTION_ROUNDS)

            from .retry import AnthropicCallError, call_anthropic_with_retry

            try:
                response = await call_anthropic_with_retry(
                    self.client,
                    model=EXECUTOR_MODEL,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                )
            except AnthropicCallError as e:
                return full_text + f"\n\nERROR: Anthropic API call failed: {e}"

            session.total_tokens_used += response.usage.input_tokens + response.usage.output_tokens

            assistant_content = []
            tool_use_blocks = []

            for block in response.content:
                if block.type == "text":
                    full_text += block.text
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    tool_use_blocks.append(block)
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                return full_text

            if tool_use_blocks:
                tool_results = []
                for block in tool_use_blocks:
                    logger.info(
                        "Session %s executor: calling %s", session.id, block.name,
                    )
                    result = await self.registry.dispatch(block.name, block.input)
                    result_text = _extract_text(result)
                    is_error = result.get("is_error", False)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                        "is_error": is_error,
                    })
                    session.tool_calls_made += 1

                messages.append({"role": "user", "content": tool_results})
            else:
                return full_text

        return full_text + "\n\nWARNING: Executor hit round limit."

    def _parse_execution_result(self, text: str) -> "ExecutionResult":
        """Parse the executor's structured output."""
        import re

        match = re.search(r"```execution_result\s*\n(.*?)```", text, re.DOTALL)
        if not match:
            return ExecutionResult(
                success=False,
                summary="Executor did not produce structured output",
                details=text[:2000],
            )

        block = match.group(1)
        status_match = re.search(r"STATUS:\s*(\S+)", block)
        summary_match = re.search(r"SUMMARY:\s*(.+)", block)
        details_match = re.search(r"DETAILS:\s*\|?\s*\n((?:  .+\n?)+)", block)
        verify_match = re.search(r"VERIFICATION:\s*\|?\s*\n((?:  .+\n?)+)", block)

        status = status_match.group(1).upper() if status_match else "UNKNOWN"
        success = status == "SUCCESS"

        return ExecutionResult(
            success=success,
            summary=summary_match.group(1).strip() if summary_match else status,
            details=_dedent(details_match.group(1)) if details_match else "",
            verification=_dedent(verify_match.group(1)) if verify_match else "",
            needs_rollback=status == "ROLLBACK_NEEDED",
        )


# ---------------------------------------------------------------------------
# Execution result
# ---------------------------------------------------------------------------
class ExecutionResult:
    """Result of a fix execution attempt."""

    def __init__(
        self,
        success: bool,
        summary: str,
        details: str = "",
        verification: str = "",
        needs_rollback: bool = False,
        guardrail_result: GuardrailResult | None = None,
    ) -> None:
        self.success = success
        self.summary = summary
        self.details = details
        self.verification = verification
        self.needs_rollback = needs_rollback
        self.pre_state: dict[str, Any] | None = None
        self.post_state: dict[str, Any] | None = None
        self.guardrail_result = guardrail_result

    def to_slack_text(self) -> str:
        """Format for Slack posting."""
        emoji = "✅" if self.success else "❌"
        lines = [f"{emoji} *Execution Result:* {self.summary}"]

        if self.details:
            lines.append(f"\n*Details:*\n```{self.details[:1500]}```")

        if self.verification:
            lines.append(f"\n*Verification:*\n```{self.verification[:1000]}```")

        if self.needs_rollback:
            lines.append("\n⚠️ *ROLLBACK NEEDED* — the fix did not resolve the issue.")

        if self.guardrail_result and self.guardrail_result.warnings:
            lines.append("\n*Guardrail Warnings:*")
            for w in self.guardrail_result.warnings:
                lines.append(f"  • {w}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_text(result: dict[str, Any]) -> str:
    """Extract text content from a tool result dict."""
    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text += block["text"]
    return text


def _dedent(text: str) -> str:
    """Remove common indentation from a text block."""
    lines = text.split("\n")
    return "\n".join(line.strip() for line in lines if line.strip())
