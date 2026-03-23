"""Triage Agent — fast alert classification using Haiku.

Single API call, no tools. Classifies alerts into specialist domains.
Falls back to deterministic routing on failure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from ...config import settings
from ...models import GrafanaAlert
from .prompts.triage_prompt import TRIAGE_SYSTEM_PROMPT, build_triage_message
from .routing import route_alert
from .tool_subsets import SpecialistDomain

logger = logging.getLogger(__name__)


@dataclass
class TriageResult:
    """Output of the triage classification."""

    domain: SpecialistDomain
    confidence: str          # "high", "medium", "low"
    reasoning: str
    priority: str            # "p1", "p2", "p3"
    source: str = "triage"   # "triage" or "deterministic"

    def to_dict(self) -> dict[str, str]:
        return {
            "domain": self.domain.value,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "priority": self.priority,
            "source": self.source,
        }


class TriageAgent:
    """Classifies alerts into specialist domains using Haiku.

    Usage:
        triage = TriageAgent()
        result = await triage.classify(alert)
        # result.domain → SpecialistDomain.POD
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.triage_model
        self._client: anthropic.Anthropic | None = None

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    async def classify(
        self,
        alert: GrafanaAlert,
        runbook_matches: list[str] | None = None,
        memory_summary: str | None = None,
    ) -> TriageResult:
        """Classify an alert into a specialist domain.

        Returns a TriageResult. On any failure, falls back to deterministic routing.
        """
        # Fast-path: if no API key, use deterministic routing
        if not settings.anthropic_api_key:
            return self._deterministic_fallback(alert, "no API key")

        try:
            return await self._classify_with_haiku(alert, runbook_matches, memory_summary)
        except Exception as e:
            logger.warning("Triage agent failed, using deterministic routing: %s", e)
            return self._deterministic_fallback(alert, str(e))

    async def _classify_with_haiku(
        self,
        alert: GrafanaAlert,
        runbook_matches: list[str] | None,
        memory_summary: str | None,
    ) -> TriageResult:
        """Call Haiku for classification."""
        import asyncio

        user_message = build_triage_message(
            alert_name=alert.alert_name,
            severity=alert.severity,
            namespace=alert.namespace,
            labels=alert.labels,
            annotations=alert.annotations,
            runbook_matches=runbook_matches,
            memory_summary=memory_summary,
        )

        client = self._get_client()

        response = await asyncio.to_thread(
            client.messages.create,
            model=self.model,
            max_tokens=256,
            system=TRIAGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        text = response.content[0].text

        # Parse the JSON response
        result = self._parse_triage_response(text, alert)

        logger.info(
            "Triage: %s → %s (confidence=%s, priority=%s, reasoning=%s)",
            alert.alert_name, result.domain.value, result.confidence,
            result.priority, result.reasoning[:80],
        )

        # If low confidence, fall through to deterministic
        if result.confidence == "low":
            deterministic = route_alert(alert)
            if deterministic != result.domain:
                logger.info(
                    "Triage low confidence: Haiku said %s, deterministic says %s — using deterministic",
                    result.domain.value, deterministic.value,
                )
                return TriageResult(
                    domain=deterministic,
                    confidence="medium",
                    reasoning=f"Deterministic override (Haiku low confidence: {result.reasoning})",
                    priority=result.priority,
                    source="deterministic",
                )

        return result

    def _parse_triage_response(self, text: str, alert: GrafanaAlert) -> TriageResult:
        """Parse Haiku's JSON response into a TriageResult."""
        # Extract JSON from possible markdown code block
        clean = text.strip()
        if "```json" in clean:
            clean = clean.split("```json")[1].split("```")[0].strip()
        elif "```" in clean:
            clean = clean.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            logger.warning("Triage: failed to parse JSON from Haiku: %s", text[:200])
            return self._deterministic_fallback(alert, "JSON parse error")

        try:
            domain = SpecialistDomain(data.get("domain", "application"))
        except ValueError:
            domain = route_alert(alert)

        return TriageResult(
            domain=domain,
            confidence=data.get("confidence", "medium"),
            reasoning=data.get("reasoning", ""),
            priority=data.get("priority", "p2"),
            source="triage",
        )

    def _deterministic_fallback(self, alert: GrafanaAlert, reason: str) -> TriageResult:
        """Fall back to regex + label routing."""
        domain = route_alert(alert)
        severity = alert.severity.lower()
        priority = "p1" if severity == "critical" else "p2" if severity == "warning" else "p3"

        return TriageResult(
            domain=domain,
            confidence="medium",
            reasoning=f"Deterministic routing ({reason})",
            priority=priority,
            source="deterministic",
        )
