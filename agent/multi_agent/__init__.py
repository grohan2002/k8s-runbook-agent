"""Multi-Agent Orchestrator — Triage → Specialist → Coordinator pipeline.

Replaces DiagnosticOrchestrator when MULTI_AGENT_ENABLED=true.
Falls back to single-agent mode on triage failure.
"""

from __future__ import annotations

import logging
from typing import Any

from ...config import settings
from ...models import GrafanaAlert
from ..correlation import alert_correlator
from ..orchestrator import DiagnosticOrchestrator
from ..prompts import format_runbook_context
from ..session import DiagnosisSession, SessionPhase, session_store
from .coordinator import CoordinatorAgent
from .specialists import SpecialistAgent, get_specialist
from .tool_subsets import SpecialistDomain
from .triage import TriageAgent

logger = logging.getLogger(__name__)


class MultiAgentOrchestrator:
    """Orchestrates the multi-agent diagnostic pipeline.

    Flow:
        1. Triage (Haiku) → classify alert into domain
        2. Specialist (Sonnet) → domain-specific diagnosis with filtered tools
        3. Coordinator (Opus) → only if correlated alerts exist

    Falls back to single-agent DiagnosticOrchestrator on triage failure
    when settings.triage_fallback_to_single is True.
    """

    def __init__(
        self,
        fallback: DiagnosticOrchestrator | None = None,
    ) -> None:
        self.triage = TriageAgent()
        self.coordinator = CoordinatorAgent()
        self.fallback = fallback or DiagnosticOrchestrator()

    async def investigate(self, alert: GrafanaAlert) -> DiagnosisSession:
        """Run the multi-agent pipeline for an alert."""

        # 1. Create session
        session = session_store.create(alert)
        session.agent_type = "multi_agent"

        from ...observability.logging import set_session_context
        set_session_context(
            session_id=session.id,
            alert_name=alert.alert_name,
            namespace=alert.namespace,
        )

        try:
            # 2. Triage — classify into specialist domain
            triage_result = await self.triage.classify(alert)
            session.specialist_domain = triage_result.domain.value
            session.triage_result = triage_result.to_dict()

            logger.info(
                "Session %s: triage → %s (confidence=%s, source=%s)",
                session.id, triage_result.domain.value,
                triage_result.confidence, triage_result.source,
            )

            # 3. Find runbook + recall incident memory (same as single-agent)
            runbook = await self._find_runbook(alert)
            session.runbook = runbook

            memory_text = await self._recall_memory(alert, session)

            # 4. Route to specialist
            specialist = get_specialist(triage_result.domain)
            await specialist.investigate(
                session, triage_result,
                runbook=runbook,
                memory_context=memory_text,
            )

            # 5. Check for coordinator activation
            await self._check_coordinator(session)

        except Exception as e:
            logger.exception("Multi-agent pipeline failed for session %s", session.id)

            # Fallback to single-agent
            if session.phase in (SessionPhase.ALERT_RECEIVED, SessionPhase.INVESTIGATING):
                logger.info("Session %s: falling back to single-agent orchestrator", session.id)
                session.agent_type = "single_fallback"
                return await self.fallback.investigate(alert)
            else:
                session.fail(str(e))

        return session

    async def _find_runbook(self, alert: GrafanaAlert):
        """Search knowledge base for a matching runbook."""
        from ...tools.knowledge_base import get_store

        store = get_store()
        matches = store.search(
            query=alert.alert_name,
            alert_name=alert.alert_name,
            labels=alert.labels,
        )
        if matches:
            best = matches[0]
            logger.info("Matched runbook '%s' (score=%.1f)", best.runbook_id, best.score)
            return store.get(best.runbook_id)
        return None

    async def _recall_memory(self, alert: GrafanaAlert, session: DiagnosisSession) -> str | None:
        """Retrieve incident memory for the specialist's context."""
        try:
            from ..incident_memory import incident_memory

            memory_ctx = await incident_memory.recall(alert)
            memory_text = incident_memory.format_for_prompt(memory_ctx)
            if memory_text:
                logger.info(
                    "Session %s: injecting incident memory (%d similar)",
                    session.id, len(memory_ctx.similar_incidents),
                )
            return memory_text
        except Exception:
            logger.warning("Session %s: incident memory recall failed", session.id, exc_info=True)
            return None

    async def _check_coordinator(self, session: DiagnosisSession) -> None:
        """Activate coordinator if correlated alerts exist."""
        if session.phase in (SessionPhase.ESCALATED, SessionPhase.FAILED):
            return  # Don't coordinate failed sessions

        correlated = alert_correlator.get_correlated_alerts(session.id)
        if not correlated:
            return

        # Get the correlated sessions
        related_sessions = []
        for ca in correlated:
            related = session_store.get(ca.session_id)
            if related and related.diagnosis and related.id != session.id:
                related_sessions.append(related)

        if not related_sessions:
            return

        logger.info(
            "Session %s: activating coordinator for %d correlated sessions",
            session.id, len(related_sessions),
        )

        # Build correlation key
        correlation_key = f"{session.alert.namespace}/{session.alert.labels.get('deployment', session.alert.labels.get('app', 'unknown'))}"

        coord_session = await self.coordinator.synthesize(
            [session] + related_sessions,
            correlation_key,
        )

        # If coordinator produced a better diagnosis, note it on the original session
        if coord_session.diagnosis:
            logger.info(
                "Coordinator produced unified diagnosis: %s",
                coord_session.diagnosis.root_cause[:100],
            )
