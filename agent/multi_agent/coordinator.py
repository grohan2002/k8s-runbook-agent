"""Coordinator Agent — synthesizes findings from multiple specialists.

Only activated for correlated multi-alert incidents. Uses Opus model.
Reads specialist diagnoses; does NOT call K8s tools directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anthropic

from ...config import settings
from ...models import Confidence, RiskLevel
from ..orchestrator import (
    _parse_diagnosis_block,
    _parse_escalate_block,
    _parse_fix_block,
)
from ..prompts import OUTPUT_FORMAT
from ..session import DiagnosisSession, SessionPhase, session_store
from .prompts.coordinator_prompt import COORDINATOR_SYSTEM_PROMPT, format_specialist_findings

logger = logging.getLogger(__name__)


class CoordinatorAgent:
    """Synthesizes findings from multiple specialist agents.

    Usage:
        coordinator = CoordinatorAgent()
        unified = await coordinator.synthesize([session1, session2], "prod/deploy/api")
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.coordinator_model
        self._client: anthropic.Anthropic | None = None

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    async def synthesize(
        self,
        sessions: list[DiagnosisSession],
        correlation_key: str,
    ) -> DiagnosisSession:
        """Merge findings from multiple specialists into a unified diagnosis.

        Creates a NEW session that represents the coordinated diagnosis.
        The original specialist sessions are kept as-is for reference.
        """
        if not sessions:
            raise ValueError("No sessions to coordinate")

        # Use the first session's alert as the primary
        primary_alert = sessions[0].alert

        # Create coordinator session
        coord_session = session_store.create(primary_alert)
        coord_session.agent_type = "coordinator"
        coord_session.specialist_domain = "coordinator"
        coord_session.transition(SessionPhase.INVESTIGATING)

        try:
            system_prompt = COORDINATOR_SYSTEM_PROMPT.format(output_format=OUTPUT_FORMAT)

            specialist_text = format_specialist_findings(sessions)

            user_message = (
                f"Multiple related alerts have fired for correlation key: {correlation_key}\n\n"
                f"{specialist_text}\n\n"
                "Synthesize these findings into a unified root cause analysis. "
                "Identify if there's a common upstream cause, or if these are truly independent issues."
            )

            coord_session.add_user_message(user_message)

            # Single API call — coordinator doesn't use tools
            response = await asyncio.to_thread(
                self._get_client().messages.create,
                model=self.model,
                max_tokens=settings.coordinator_token_budget,
                system=system_prompt,
                messages=coord_session.messages,
            )

            coord_session.total_tokens_used += (
                response.usage.input_tokens + response.usage.output_tokens
            )

            full_text = ""
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    full_text += block.text
                    assistant_content.append({"type": "text", "text": block.text})

            coord_session.add_assistant_message(assistant_content)

            # Parse the output using existing parsers
            self._process_response(coord_session, full_text)

            logger.info(
                "Coordinator synthesized %d specialist sessions for key '%s' → phase=%s",
                len(sessions), correlation_key, coord_session.phase.value,
            )

        except Exception as e:
            logger.exception("Coordinator failed for key '%s'", correlation_key)
            coord_session.escalate(f"Coordinator error: {e}")

        return coord_session

    def _process_response(self, session: DiagnosisSession, text: str) -> None:
        """Parse coordinator's structured output."""
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
            session.escalate("Coordinator produced no parseable output.")
