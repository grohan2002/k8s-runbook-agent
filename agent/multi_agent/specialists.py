"""Specialist agents — domain-specific diagnostic Claude loops.

Each specialist gets a filtered tool subset and focused system prompt.
The investigation loop reuses the same pattern as DiagnosticOrchestrator._run_loop.
Output format is identical — existing parsers work unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from ...config import settings
from ...models import Confidence, GrafanaAlert, RiskLevel
from ..orchestrator import (
    MAX_TOKENS,
    MAX_TOOL_ROUNDS,
    _parse_diagnosis_block,
    _parse_escalate_block,
    _parse_fix_block,
)
from ..prompts import OUTPUT_FORMAT, format_alert_context, format_runbook_context
from ..session import DiagnosisSession, SessionPhase
from ..tool_registry import ToolRegistry, build_domain_registry
from .prompts.app_prompt import APP_SYSTEM_PROMPT
from .prompts.infra_prompt import INFRA_SYSTEM_PROMPT
from .prompts.network_prompt import NETWORK_SYSTEM_PROMPT
from .prompts.pod_prompt import POD_SYSTEM_PROMPT
from .tool_subsets import SpecialistDomain, get_domain_tool_names
from .triage import TriageResult

logger = logging.getLogger(__name__)

# Domain → base system prompt
DOMAIN_PROMPTS: dict[SpecialistDomain, str] = {
    SpecialistDomain.POD: POD_SYSTEM_PROMPT,
    SpecialistDomain.NETWORK: NETWORK_SYSTEM_PROMPT,
    SpecialistDomain.INFRASTRUCTURE: INFRA_SYSTEM_PROMPT,
    SpecialistDomain.APPLICATION: APP_SYSTEM_PROMPT,
}


class SpecialistAgent:
    """Domain-specific diagnostic agent with filtered tools.

    Usage:
        agent = SpecialistAgent(SpecialistDomain.POD)
        session = await agent.investigate(session, triage_result)
    """

    def __init__(
        self,
        domain: SpecialistDomain,
        model: str | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.domain = domain
        self.model = model or settings.specialist_model
        self._client: anthropic.Anthropic | None = None
        self._registry = registry

    @property
    def registry(self) -> ToolRegistry:
        if self._registry is None:
            tool_names = get_domain_tool_names(self.domain)
            self._registry = build_domain_registry(tool_names)
        return self._registry

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def build_system_prompt(
        self,
        alert: GrafanaAlert,
        triage_result: TriageResult,
        runbook=None,
        memory_context: str | None = None,
    ) -> str:
        """Assemble the specialist's system prompt."""
        base_prompt = DOMAIN_PROMPTS[self.domain].format(output_format=OUTPUT_FORMAT)

        sections = [base_prompt]

        # Triage context
        sections.append(
            f"## Triage Context\n"
            f"You were selected by the triage agent because: {triage_result.reasoning}\n"
            f"Priority: {triage_result.priority}\n"
            f"Confidence: {triage_result.confidence}"
        )

        # Incident memory
        if memory_context:
            sections.append(memory_context)

        # Alert details
        sections.append(format_alert_context(alert))

        # Runbook
        if runbook:
            sections.append(format_runbook_context(runbook))

        return "\n\n".join(sections)

    async def investigate(
        self,
        session: DiagnosisSession,
        triage_result: TriageResult,
        runbook=None,
        memory_context: str | None = None,
    ) -> DiagnosisSession:
        """Run the specialist's diagnostic loop."""
        session.transition(SessionPhase.INVESTIGATING)

        system_prompt = self.build_system_prompt(
            session.alert, triage_result, runbook, memory_context
        )

        # Seed conversation
        opening = self._build_opening(session.alert, triage_result)
        session.add_user_message(opening)

        # Run tool loop
        await self._run_loop(session, system_prompt)

        return session

    async def _run_loop(self, session: DiagnosisSession, system_prompt: str) -> None:
        """Multi-turn conversation loop — same pattern as DiagnosticOrchestrator."""
        tools = self.registry.to_anthropic_tools()
        rounds = 0
        token_budget = settings.max_tokens_per_session

        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1

            if token_budget > 0 and session.total_tokens_used >= token_budget:
                session.escalate(
                    f"Token budget exhausted ({session.total_tokens_used:,}/{token_budget:,})"
                )
                return

            logger.info(
                "Session %s [%s]: round %d/%d (%d messages)",
                session.id, self.domain.value, rounds, MAX_TOOL_ROUNDS, len(session.messages),
            )

            # Call Claude
            from ..retry import AnthropicCallError, call_anthropic_with_retry

            try:
                response = await call_anthropic_with_retry(
                    self._get_client(),
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    tools=tools,
                    messages=session.messages,
                )
            except AnthropicCallError as e:
                session.escalate(f"AI service unavailable: {e}")
                return

            session.total_tokens_used += response.usage.input_tokens + response.usage.output_tokens

            # Process response
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

            session.add_assistant_message(assistant_content)

            if response.stop_reason == "end_turn":
                enforcement_rounds = getattr(session, "_enforcement_rounds", 0)
                if enforcement_rounds < 3:
                    from .tool_subsets import check_required_tools_met
                    met, missing = check_required_tools_met(self.domain, session.tools_called)
                    if not met:
                        session._enforcement_rounds = enforcement_rounds + 1
                        from ...observability.metrics import enforcement_triggered
                        enforcement_triggered.inc({"agent_type": self.domain.value, "round": str(session._enforcement_rounds)})
                        logger.info(
                            "Session %s [%s]: enforcement round %d/3, missing: %s",
                            session.id, self.domain.value, session._enforcement_rounds, missing,
                        )
                        session.add_user_message(
                            f"STOP — you have not called these required tools: {', '.join(sorted(missing))}. "
                            f"Call them now before providing your diagnosis."
                        )
                        continue

                self._process_final(session, full_text)
                await self._score_fix_confidence(session)
                return

            if tool_use_blocks:
                await self._execute_tools(session, tool_use_blocks)
            else:
                self._process_final(session, full_text)
                return

        session.escalate(f"Specialist [{self.domain.value}] exceeded {MAX_TOOL_ROUNDS} rounds")

    async def _execute_tools(self, session: DiagnosisSession, blocks: list) -> None:
        """Execute tool calls and add results to conversation."""
        tool_results = []
        for block in blocks:
            logger.info("Session %s [%s]: tool %s", session.id, self.domain.value, block.name)
            result = await self.registry.dispatch(block.name, block.input)

            result_text = ""
            is_error = result.get("is_error", False)
            for cb in result.get("content", []):
                if cb.get("type") == "text":
                    result_text += cb["text"]

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
                "is_error": is_error,
            })
            session.tool_calls_made += 1
            session.tools_called.add(block.name)

        session.messages.append({"role": "user", "content": tool_results})

    def _process_final(self, session: DiagnosisSession, text: str) -> None:
        """Parse structured output — reuses existing parsers."""
        escalate = _parse_escalate_block(text)
        if escalate:
            session.escalate(escalate["reason"])
            return

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

        if not diag and not fix and not escalate:
            session.escalate("Specialist completed but produced no parseable output.")

    async def _score_fix_confidence(self, session: DiagnosisSession) -> None:
        """Calculate composite confidence score for the proposed fix."""
        if not session.diagnosis or not session.fix_proposal:
            return
        try:
            from ..incident_memory import incident_memory
            session.fix_confidence = await incident_memory.get_fix_confidence(
                alert_name=session.alert.alert_name,
                fix_summary=session.fix_proposal.summary,
                diagnosis_confidence=session.diagnosis.confidence,
                evidence_count=len(session.diagnosis.evidence),
            )
            logger.info(
                "Session %s [%s]: fix confidence %d%%",
                session.id, self.domain.value, session.fix_confidence.percentage,
            )
        except Exception:
            logger.warning("Fix confidence scoring failed for %s", session.id, exc_info=True)

    def _build_opening(self, alert: GrafanaAlert, triage: TriageResult) -> str:
        lines = [
            f"A Grafana alert has fired. You are the {self.domain.value} specialist.\n",
            f"Alert: {alert.alert_name}",
            f"Severity: {alert.severity}",
            f"Summary: {alert.summary}",
            f"Namespace: {alert.namespace}",
        ]
        if alert.pod:
            lines.append(f"Pod: {alert.pod}")
        if alert.labels:
            lines.append(f"\nLabels: {json.dumps(alert.labels)}")
        lines.append(
            "\nInvestigate using your tools and produce a diagnosis + fix proposal."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_specialists: dict[SpecialistDomain, SpecialistAgent] = {}


def get_specialist(domain: SpecialistDomain) -> SpecialistAgent:
    """Get or create a specialist agent for a domain (singleton per domain)."""
    if domain not in _specialists:
        _specialists[domain] = SpecialistAgent(domain)
    return _specialists[domain]
