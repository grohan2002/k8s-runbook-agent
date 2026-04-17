"""Orchestrator — the agentic diagnosis loop.

Drives Claude through a multi-turn conversation where it:
1. Receives an alert
2. Searches for a matching runbook
3. Calls inspection tools to investigate the cluster
4. Produces a structured diagnosis
5. Proposes a fix for human approval

The loop runs until Claude emits a `stop_reason: "end_turn"` (diagnosis complete),
hits the tool-call budget, or encounters an unrecoverable error.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

from ..config import settings
from ..models import Confidence, GrafanaAlert, RiskLevel
from ..observability.logging import set_session_context
from ..observability.metrics import (
    anthropic_call_duration,
    anthropic_calls,
    diagnoses_completed,
    diagnosis_duration,
    escalations,
    tokens_used,
    tool_calls as tool_calls_metric,
    Timer,
)
from .prompts import build_system_prompt, format_runbook_context
from .session import DiagnosisSession, SessionPhase, session_store
from .tool_registry import ToolRegistry, build_default_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MODEL = "claude-sonnet-4-20250514"
MAX_TOOL_ROUNDS = 25  # Safety cap on tool-use turns (prevents runaway loops)
MAX_TOKENS = 4096


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------
def _parse_diagnosis_block(text: str) -> dict[str, Any] | None:
    """Extract structured diagnosis from Claude's response text."""
    match = re.search(r"```diagnosis\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        return None

    block = match.group(1)
    result: dict[str, Any] = {}

    # ROOT_CAUSE
    m = re.search(r"ROOT_CAUSE:\s*(.+)", block)
    if m:
        result["root_cause"] = m.group(1).strip()

    # CONFIDENCE
    m = re.search(r"CONFIDENCE:\s*(HIGH|MEDIUM|LOW)", block, re.IGNORECASE)
    if m:
        result["confidence"] = m.group(1).upper()

    # EVIDENCE (multi-line list)
    m = re.search(r"EVIDENCE:\s*\n((?:\s*-\s*.+\n?)+)", block)
    if m:
        result["evidence"] = [
            line.strip().lstrip("- ") for line in m.group(1).strip().split("\n") if line.strip()
        ]

    # RULED_OUT (multi-line list)
    m = re.search(r"RULED_OUT:\s*\n((?:\s*-\s*.+\n?)+)", block)
    if m:
        result["ruled_out"] = [
            line.strip().lstrip("- ") for line in m.group(1).strip().split("\n") if line.strip()
        ]

    return result if "root_cause" in result else None


def _parse_fix_block(text: str) -> dict[str, Any] | None:
    """Extract structured fix proposal from Claude's response text."""
    match = re.search(r"```fix_proposal\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        return None

    block = match.group(1)
    result: dict[str, Any] = {}

    # Simple single-line fields
    for field in ("SUMMARY", "RISK"):
        m = re.search(rf"{field}:\s*(.+)", block)
        if m:
            result[field.lower()] = m.group(1).strip()

    # Multi-line fields (YAML-style block scalars)
    for field in ("DESCRIPTION", "DRY_RUN", "ROLLBACK"):
        m = re.search(rf"{field}:\s*\|?\s*\n((?:  .+\n?)+)", block)
        if m:
            # De-indent the block
            lines = m.group(1).split("\n")
            result[field.lower()] = "\n".join(line.strip() for line in lines if line.strip())

    # HUMAN_VALUES_NEEDED list
    m = re.search(r"HUMAN_VALUES_NEEDED:\s*\n((?:\s*-\s*.+\n?)+)", block)
    if m:
        result["human_values"] = [
            line.strip().lstrip("- ") for line in m.group(1).strip().split("\n") if line.strip()
        ]

    return result if "summary" in result else None


def _parse_escalate_block(text: str) -> dict[str, Any] | None:
    """Extract escalation request from Claude's response text."""
    match = re.search(r"```escalate\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        return None

    block = match.group(1)
    m = re.search(r"REASON:\s*(.+)", block)
    return {"reason": m.group(1).strip()} if m else {"reason": "Agent requested escalation"}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class DiagnosticOrchestrator:
    """Drives the agentic diagnosis loop for one alert.

    Usage:
        orch = DiagnosticOrchestrator()
        session = await orch.investigate(alert)
        # session now contains diagnosis + fix_proposal (or escalation)
    """

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.registry = registry or build_default_registry()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    async def investigate(self, alert: GrafanaAlert) -> DiagnosisSession:
        """Run a full diagnosis for an alert. Returns the completed session."""

        # 1. Create session
        session = session_store.create(alert)
        session.transition(SessionPhase.INVESTIGATING)

        # Set correlation context for all logs in this investigation
        set_session_context(
            session_id=session.id,
            alert_name=alert.alert_name,
            namespace=alert.namespace,
        )

        try:
            with Timer(diagnosis_duration):
                # 2. Search for a matching runbook
                runbook = await self._find_runbook(alert, session)
                session.runbook = runbook

                # 2.5. Retrieve incident memory (past similar incidents)
                memory_text = None
                try:
                    from .incident_memory import incident_memory

                    memory_ctx = await incident_memory.recall(alert)
                    memory_text = incident_memory.format_for_prompt(memory_ctx)
                    if memory_text:
                        logger.info(
                            "Session %s: injecting incident memory (%d similar, %d rates, %d patterns)",
                            session.id,
                            len(memory_ctx.similar_incidents),
                            len(memory_ctx.fix_success_rates),
                            len(memory_ctx.recurring_patterns),
                        )
                except Exception:
                    logger.warning("Session %s: incident memory recall failed", session.id, exc_info=True)

                # 3. Build system prompt
                system_prompt = build_system_prompt(alert, runbook, memory_context=memory_text)

                # 4. Seed the conversation with the alert as the first user message
                opening_message = await self._build_opening_message(alert, runbook, session=session)
                session.add_user_message(opening_message)

                # 5. Run the agentic loop
                await self._run_loop(session, system_prompt)

        except Exception as e:
            logger.exception("Orchestrator failed for session %s", session.id)
            session.fail(str(e))

        # Record outcome metrics
        if session.diagnosis:
            diagnoses_completed.inc({
                "confidence": session.diagnosis.confidence.value,
                "alert_name": alert.alert_name,
            })
        if session.phase == SessionPhase.ESCALATED:
            escalations.inc({"reason_category": "agent"})
        if session.total_tokens_used:
            tokens_used.inc({"model": MODEL}, value=session.total_tokens_used)

        return session

    # ------------------------------------------------------------------
    # Agentic loop
    # ------------------------------------------------------------------
    async def _run_loop(self, session: DiagnosisSession, system_prompt: str) -> None:
        """Multi-turn conversation loop with tool use."""

        tools = self.registry.to_anthropic_tools()
        rounds = 0

        token_budget = settings.max_tokens_per_session

        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1

            # Conversation pruning — keep context manageable
            if len(session.messages) > 20:
                self._prune_conversation(session)

            # Token budget check
            if token_budget > 0 and session.total_tokens_used >= token_budget:
                logger.warning(
                    "Session %s: token budget exhausted (%d/%d)",
                    session.id, session.total_tokens_used, token_budget,
                )
                session.escalate(
                    f"Token budget exhausted ({session.total_tokens_used:,} / {token_budget:,} tokens). "
                    "Escalating for manual investigation."
                )
                return

            logger.info(
                "Session %s: round %d/%d (%d messages)",
                session.id, rounds, MAX_TOOL_ROUNDS, len(session.messages),
            )

            # Call Claude with retry logic + metrics
            from .retry import AnthropicCallError, call_anthropic_with_retry

            try:
                with Timer(anthropic_call_duration):
                    response = await call_anthropic_with_retry(
                        self.client,
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=system_prompt,
                        tools=tools,
                        messages=session.messages,
                    )
                anthropic_calls.inc({"model": MODEL, "status": "ok"})
            except AnthropicCallError as e:
                anthropic_calls.inc({"model": MODEL, "status": "error"})
                logger.error("Session %s: Anthropic API call failed after retries: %s", session.id, e)
                session.escalate(f"AI service unavailable: {e}")
                return

            # Track token usage
            session.total_tokens_used += response.usage.input_tokens + response.usage.output_tokens

            # Process the response content blocks
            assistant_content = []
            tool_use_blocks = []
            full_text = ""

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

            # Store the assistant message
            session.add_assistant_message(assistant_content)

            # If stop_reason is "end_turn", Claude is done — but enforce tool calls first
            if response.stop_reason == "end_turn":
                enforcement_rounds = getattr(session, "_enforcement_rounds", 0)
                if enforcement_rounds < 3:
                    from .multi_agent.tool_subsets import check_required_tools_met, MAX_ENFORCEMENT_ROUNDS
                    met, missing = check_required_tools_met(None, session.tools_called)
                    if not met:
                        session._enforcement_rounds = enforcement_rounds + 1
                        from ..observability.metrics import enforcement_triggered
                        enforcement_triggered.inc({"agent_type": "single", "round": str(session._enforcement_rounds)})
                        logger.info(
                            "Session %s: enforcement round %d/%d, missing tools: %s",
                            session.id, session._enforcement_rounds, MAX_ENFORCEMENT_ROUNDS, missing,
                        )
                        session.add_user_message(
                            f"STOP — you have not called these required tools: {', '.join(sorted(missing))}. "
                            f"Call them now before providing your diagnosis."
                        )
                        continue

                self._process_final_response(session, full_text)
                # Score fix confidence (Feature 3)
                await self._score_fix_confidence(session)
                # Verification loop (Feature 2)
                if await self._verify_and_maybe_retry(session):
                    continue  # retry with reviewer feedback
                return

            # If there are tool calls, execute them and continue the loop
            if tool_use_blocks:
                await self._execute_tools(session, tool_use_blocks)
            else:
                # No tool calls and not end_turn — shouldn't happen, but handle gracefully
                logger.warning("Session %s: unexpected stop_reason=%s", session.id, response.stop_reason)
                self._process_final_response(session, full_text)
                return

        # Ran out of rounds
        logger.warning("Session %s: hit MAX_TOOL_ROUNDS (%d)", session.id, MAX_TOOL_ROUNDS)
        session.escalate(f"Investigation exceeded {MAX_TOOL_ROUNDS} tool rounds without reaching a conclusion.")

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------
    async def _execute_tools(
        self,
        session: DiagnosisSession,
        tool_use_blocks: list,
    ) -> None:
        """Execute tool calls and add results to the conversation."""

        # Build a single user message with all tool results
        tool_results = []

        for block in tool_use_blocks:
            logger.info(
                "Session %s: calling tool %s with %s",
                session.id, block.name, json.dumps(block.input, default=str)[:200],
            )

            result = await self.registry.dispatch(block.name, block.input)

            # Extract text from result content blocks
            result_text = ""
            is_error = result.get("is_error", False)
            for content_block in result.get("content", []):
                if content_block.get("type") == "text":
                    result_text += content_block["text"]

            # Redact secrets/PII before passing back to Claude (defense in depth)
            if settings.redaction_enabled:
                from .redaction import redact
                redaction = redact(result_text)
                if redaction.had_secrets:
                    logger.info(
                        "Session %s: redacted %d secret(s) from %s output (%s)",
                        session.id, redaction.redaction_count, block.name,
                        ",".join(redaction.redactions.keys()),
                    )
                    try:
                        from ..observability.metrics import secrets_redacted
                        for kind, count in redaction.redactions.items():
                            secrets_redacted.inc({"kind": kind, "source": block.name}, value=count)
                    except Exception:
                        pass
                    result_text = redaction.text

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
                "is_error": is_error,
            })

            session.tool_calls_made += 1
            session.tools_called.add(block.name)
            tool_calls_metric.inc({
                "tool_name": block.name,
                "status": "error" if is_error else "ok",
            })

            logger.debug(
                "Session %s: tool %s returned %d chars (error=%s)",
                session.id, block.name, len(result_text), is_error,
            )

        # Add all tool results as a single user message
        session.messages.append({"role": "user", "content": tool_results})

    # ------------------------------------------------------------------
    # Parse final response
    # ------------------------------------------------------------------
    def _process_final_response(self, session: DiagnosisSession, text: str) -> None:
        """Parse Claude's final response into structured diagnosis/fix/escalation."""

        # Try to parse escalation first
        escalate = _parse_escalate_block(text)
        if escalate:
            session.escalate(escalate["reason"])
            return

        # Parse diagnosis
        diag = _parse_diagnosis_block(text)
        if diag:
            try:
                confidence = Confidence(diag.get("confidence", "low").lower())
            except ValueError:
                confidence = Confidence.LOW

            session.set_diagnosis(
                root_cause=diag["root_cause"],
                confidence=confidence,
                evidence=diag.get("evidence", []),
                ruled_out=diag.get("ruled_out", []),
            )

        # Parse fix proposal
        fix = _parse_fix_block(text)
        if fix:
            try:
                risk = RiskLevel(fix.get("risk", "high").lower())
            except ValueError:
                risk = RiskLevel.HIGH

            human_values = fix.get("human_values", [])
            session.set_fix_proposal(
                summary=fix.get("summary", ""),
                description=fix.get("description", ""),
                risk_level=risk,
                dry_run_output=fix.get("dry_run", ""),
                rollback_plan=fix.get("rollback", ""),
                requires_human_values=bool(human_values),
                human_value_fields=human_values,
            )
            session.request_approval()

        # If neither diagnosis nor fix was parsed, escalate
        if not diag and not fix and not escalate:
            logger.warning("Session %s: could not parse structured output from Claude", session.id)
            # Store the raw text as evidence anyway
            session.escalate(
                "Agent completed investigation but did not produce a parseable diagnosis. "
                "Raw output stored in conversation history."
            )

    # ------------------------------------------------------------------
    # Runbook search
    # ------------------------------------------------------------------
    async def _find_runbook(self, alert: GrafanaAlert, session: DiagnosisSession):
        """Search the knowledge base for a matching runbook."""
        from ..tools.knowledge_base import get_store

        store = get_store()
        matches = store.search(
            query=alert.alert_name,
            alert_name=alert.alert_name,
            labels=alert.labels,
        )
        if matches:
            best = matches[0]
            logger.info(
                "Session %s: matched runbook '%s' (score=%.1f)",
                session.id, best.runbook_id, best.score,
            )
            return store.get(best.runbook_id)
        return None

    # ------------------------------------------------------------------
    # Opening message
    # ------------------------------------------------------------------
    def _prune_conversation(self, session: DiagnosisSession) -> None:
        """Prune conversation history to keep context manageable.

        Strategy:
          - Keep the first 4 messages (opening context + first tool calls)
          - Summarize middle messages into a single condensed message
          - Keep the last 8 messages (recent investigation state)
        """
        msgs = session.messages
        if len(msgs) <= 20:
            return

        keep_start = 4
        keep_end = 8
        middle = msgs[keep_start:-keep_end]

        # Count what was in the middle
        tool_calls = sum(
            1 for m in middle
            if isinstance(m.get("content"), list)
            and any(c.get("type") == "tool_result" for c in m["content"])
        )
        assistant_msgs = sum(1 for m in middle if m.get("role") == "assistant")

        summary = (
            f"[CONTEXT PRUNED: {len(middle)} messages summarized — "
            f"{tool_calls} tool results, {assistant_msgs} assistant turns. "
            f"Key findings from earlier investigation are reflected in recent messages.]"
        )

        session.messages = (
            msgs[:keep_start]
            + [{"role": "user", "content": summary}]
            + msgs[-keep_end:]
        )

        logger.info(
            "Session %s: pruned conversation from %d to %d messages",
            session.id, len(msgs), len(session.messages),
        )

    # ------------------------------------------------------------------
    # Fix confidence scoring (Feature 3)
    # ------------------------------------------------------------------
    async def _score_fix_confidence(self, session: DiagnosisSession) -> None:
        """Calculate composite confidence score for the proposed fix."""
        if not session.diagnosis or not session.fix_proposal:
            return
        try:
            from .incident_memory import incident_memory
            session.fix_confidence = await incident_memory.get_fix_confidence(
                alert_name=session.alert.alert_name,
                fix_summary=session.fix_proposal.summary,
                diagnosis_confidence=session.diagnosis.confidence,
                evidence_count=len(session.diagnosis.evidence),
            )
            from ..observability.metrics import fix_confidence_histogram
            fix_confidence_histogram.observe(session.fix_confidence.score)
            logger.info(
                "Session %s: fix confidence %d%% (history=%s)",
                session.id, session.fix_confidence.percentage, session.fix_confidence.has_history,
            )
        except Exception:
            logger.warning("Fix confidence scoring failed for %s", session.id, exc_info=True)

    # ------------------------------------------------------------------
    # Verification loop (Feature 2)
    # ------------------------------------------------------------------
    async def _verify_and_maybe_retry(self, session: DiagnosisSession) -> bool:
        """Run verification reviewer. Returns True if loop should continue (retry)."""
        if not settings.fix_verification_enabled:
            return False
        if not session.fix_proposal or session.phase == SessionPhase.ESCALATED:
            return False
        if getattr(session, "_verification_retried", False):
            return False  # already retried once

        try:
            from .verification import VerificationVerdict, extract_tool_results_summary, verify_fix
            tool_summary = extract_tool_results_summary(session)
            result = await verify_fix(session, tool_summary)
            logger.info("Session %s: verification verdict=%s", session.id, result.verdict.value)

            from ..observability.metrics import verification_overrides

            if result.verdict == VerificationVerdict.REVISE:
                session._verification_retried = True
                verification_overrides.inc({"verdict": "revise"})
                session.add_user_message(
                    f"REVIEWER FEEDBACK: {result.feedback}\n\n"
                    "Revise your diagnosis and/or fix proposal based on this feedback."
                )
                return True  # signal caller to continue the loop

            if result.verdict == VerificationVerdict.REJECT:
                verification_overrides.inc({"verdict": "reject"})
                session.escalate(f"Fix rejected by reviewer: {result.feedback}")
                return False

            # APPROVE — proceed normally
            return False
        except Exception:
            logger.warning("Verification failed for %s, proceeding anyway", session.id, exc_info=True)
            return False  # fail-open

    async def _build_opening_message(self, alert: GrafanaAlert, runbook=None, session: DiagnosisSession | None = None) -> str:
        """Build the first user message with pre-fetched context.

        Pre-fetches pod status and recent events so Claude starts with baseline
        knowledge instead of wasting tool calls on basic discovery.

        Untrusted fields (summary, annotations) are sanitized for prompt
        injection before being embedded in Claude's context.
        """
        from .prompt_safety import scan_and_wrap

        # Sanitize user-controlled fields (summary from annotations)
        safe_summary = alert.summary
        if settings.prompt_safety_enabled and alert.summary:
            safe_summary, safety_result = scan_and_wrap(alert.summary, source="alert_summary")
            if safety_result.had_threats:
                logger.warning(
                    "Session %s: alert summary has injection risk=%s, matches=%s, unicode_tags=%d",
                    session.id if session else "unknown", safety_result.risk.value,
                    list(safety_result.matches.keys()), safety_result.stripped_unicode_tags,
                )
                try:
                    from ..observability.metrics import prompt_injections_detected
                    prompt_injections_detected.inc({
                        "source": "alert_summary",
                        "risk": safety_result.risk.value,
                    })
                except Exception:
                    pass

        lines = [
            f"A Grafana alert has fired. Please investigate and diagnose the issue.\n",
            f"Alert: {alert.alert_name}",
            f"Severity: {alert.severity}",
            f"Summary: {safe_summary}",
            f"Namespace: {alert.namespace}",
        ]
        if alert.pod:
            lines.append(f"Pod: {alert.pod}")

        if alert.labels:
            # Labels come from Prometheus/alert rules — generally trusted, but still strip unicode tags
            labels_str = json.dumps(alert.labels)
            if settings.prompt_safety_enabled:
                from .prompt_safety import scan
                label_safety = scan(labels_str)
                labels_str = label_safety.sanitized_text
            lines.append(f"\nLabels: {labels_str}")

        # Pre-fetch basic context to save Claude 2-3 tool calls
        try:
            if alert.pod:
                result = await self.registry.dispatch("get_pod_status", {
                    "namespace": alert.namespace,
                    "pod_name": alert.pod,
                })
                if not result.get("is_error"):
                    pod_text = ""
                    for block in result.get("content", []):
                        if block.get("type") == "text":
                            pod_text += block["text"]
                    if pod_text:
                        lines.append(f"\n## Pre-fetched Pod Status\n{pod_text[:1500]}")

            # Pre-fetch recent events
            result = await self.registry.dispatch("get_events", {
                "namespace": alert.namespace,
                "since_minutes": "15",
            })
            if not result.get("is_error"):
                events_text = ""
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        events_text += block["text"]
                if events_text:
                    lines.append(f"\n## Pre-fetched Events\n{events_text[:1500]}")
        except Exception:
            logger.debug("Pre-fetch failed for opening message — Claude will discover via tools")

        lines.append(
            "\nThe pod status and events above are pre-fetched for context. "
            "You still MUST call the required inspection tools (get_pod_status, get_pod_logs, get_events, etc.) "
            "to get full details before diagnosing. Start your investigation now."
        )

        return "\n".join(lines)
