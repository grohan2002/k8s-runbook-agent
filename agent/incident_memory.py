"""Incident memory — pgvector RAG for learning from past incidents.

After each resolved session, the agent records what happened (alert pattern,
root cause, fix applied, outcome) with a vector embedding. Before each new
investigation, it retrieves semantically similar past incidents and injects
them into Claude's system prompt.

This gives the agent "memory" — it can say:
  - "This fix worked 5/5 times for this alert type"
  - "This workload has had 3 OOMKilled incidents this week"
  - "A similar issue was resolved by increasing memory to 512Mi"

Architecture:
  - Embeddings: Voyage AI (voyage-3, 1024 dims) via Anthropic API key
  - Storage: PostgreSQL with pgvector extension
  - Fallback: tsvector full-text search when embeddings unavailable
  - Integration: injected into system prompt via build_system_prompt()

Usage:
    from .incident_memory import incident_memory

    # Record after resolution
    await incident_memory.record(session)

    # Recall before diagnosis
    ctx = await incident_memory.recall(alert)
    prompt_text = incident_memory.format_for_prompt(ctx)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import settings
from ..models import GrafanaAlert
from .session import DiagnosisSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes for memory context
# ---------------------------------------------------------------------------
@dataclass
class PastIncident:
    """A single past incident retrieved from memory."""

    session_id: str
    alert_name: str
    namespace: str
    workload_key: str | None
    root_cause: str
    confidence: str
    fix_summary: str
    fix_risk_level: str
    outcome: str
    execution_result: str | None
    resolved_at: datetime | None
    similarity: float = 0.0


@dataclass
class FixSuccessRate:
    """Success/failure stats for a specific fix type."""

    fix_summary: str
    total: int
    successes: int
    failures: int
    rejections: int

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total > 0 else 0.0

    @property
    def rate_str(self) -> str:
        pct = round(self.success_rate * 100)
        return f"{self.successes}/{self.total} ({pct}%)"


@dataclass
class RecurringPattern:
    """A detected recurring alert pattern on a workload."""

    alert_name: str
    root_cause: str
    occurrences: int
    first_seen: datetime | None
    last_seen: datetime | None
    fixes_tried: list[str] = field(default_factory=list)


@dataclass
class MemoryContext:
    """Complete memory context for a single alert investigation."""

    similar_incidents: list[PastIncident] = field(default_factory=list)
    fix_success_rates: list[FixSuccessRate] = field(default_factory=list)
    recurring_patterns: list[RecurringPattern] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.similar_incidents or self.fix_success_rates or self.recurring_patterns)


# ---------------------------------------------------------------------------
# Incident Memory
# ---------------------------------------------------------------------------
class IncidentMemory:
    """Records and recalls past incidents for the diagnostic agent."""

    async def record(self, session: DiagnosisSession) -> None:
        """Extract and persist a memory entry from a resolved session.

        Called automatically on terminal phase transitions via session.py.
        """
        if not settings.incident_memory_enabled:
            return

        # Only record sessions that have a diagnosis
        if not session.diagnosis:
            logger.debug("Skipping memory record for %s — no diagnosis", session.id)
            return

        from ..db import save_incident_memory
        from .correlation import _extract_workload_key
        from .embeddings import build_incident_text, embedding_provider

        alert = session.alert
        diag = session.diagnosis
        fix = session.fix_proposal

        # Determine outcome
        outcome = self._determine_outcome(session)

        # Build embedding text
        embed_text = build_incident_text(
            alert_name=alert.alert_name,
            namespace=alert.namespace,
            root_cause=diag.root_cause,
            fix_summary=fix.summary if fix else "no fix proposed",
        )

        # Get vector embedding (None if unavailable — fallback to tsvector)
        embedding = await embedding_provider.embed(embed_text)

        # Extract workload key for recurring pattern detection
        workload_key = _extract_workload_key(alert)

        await save_incident_memory(
            session_id=session.id,
            alert_name=alert.alert_name,
            namespace=alert.namespace,
            workload_key=workload_key,
            root_cause=diag.root_cause,
            confidence=diag.confidence.value,
            evidence=diag.evidence,
            fix_summary=fix.summary if fix else "no fix proposed",
            fix_description=fix.description if fix else "",
            fix_risk_level=fix.risk_level.value if fix else "unknown",
            runbook_id=session.runbook.metadata.id if session.runbook else None,
            outcome=outcome,
            execution_result=session.approval.execution_result,
            embedding=embedding,
            resolved_at=session.updated_at,
        )

        logger.info(
            "Incident memory recorded: session=%s alert=%s outcome=%s embedding=%s",
            session.id, alert.alert_name, outcome,
            "yes" if embedding else "no (tsvector fallback)",
        )

    async def recall(self, alert: GrafanaAlert, limit: int | None = None) -> MemoryContext:
        """Retrieve similar past incidents for an alert.

        Uses vector similarity when embeddings are available,
        falls back to structural + tsvector search otherwise.
        """
        if not settings.incident_memory_enabled:
            return MemoryContext()

        from ..db import (
            find_similar_incidents_by_text,
            find_similar_incidents_by_vector,
            get_fix_success_rates,
            get_recurring_patterns,
        )
        from .correlation import _extract_workload_key
        from .embeddings import build_incident_text, embedding_provider

        recall_limit = limit or settings.incident_memory_recall_limit
        workload_key = _extract_workload_key(alert)

        # Try vector search first
        similar_rows: list[dict[str, Any]] = []

        if embedding_provider.available:
            query_text = build_incident_text(
                alert_name=alert.alert_name,
                namespace=alert.namespace,
                root_cause=alert.summary,  # Use alert summary as proxy for root cause
                fix_summary="",
            )
            query_embedding = await embedding_provider.embed(query_text)
            if query_embedding:
                similar_rows = await find_similar_incidents_by_vector(
                    embedding=query_embedding,
                    limit=recall_limit,
                )

        # Fallback to text search if vector search returned nothing
        if not similar_rows:
            similar_rows = await find_similar_incidents_by_text(
                alert_name=alert.alert_name,
                workload_key=workload_key,
                search_query=f"{alert.alert_name} {alert.namespace} {alert.summary}",
                limit=recall_limit,
            )

        # Convert rows to typed objects
        similar_incidents = [
            PastIncident(
                session_id=row.get("session_id", ""),
                alert_name=row.get("alert_name", ""),
                namespace=row.get("namespace", ""),
                workload_key=row.get("workload_key"),
                root_cause=row.get("root_cause", ""),
                confidence=row.get("confidence", ""),
                fix_summary=row.get("fix_summary", ""),
                fix_risk_level=row.get("fix_risk_level", ""),
                outcome=row.get("outcome", ""),
                execution_result=row.get("execution_result"),
                resolved_at=row.get("resolved_at"),
                similarity=float(row.get("similarity", 0)),
            )
            for row in similar_rows
        ]

        # Get fix success rates
        rate_rows = await get_fix_success_rates(alert.alert_name)
        fix_rates = [
            FixSuccessRate(
                fix_summary=row["fix_summary"],
                total=row["total"],
                successes=row["successes"],
                failures=row["failures"],
                rejections=row.get("rejections", 0),
            )
            for row in rate_rows
        ]

        # Check for recurring patterns
        patterns: list[RecurringPattern] = []
        if workload_key:
            pattern_rows = await get_recurring_patterns(
                workload_key=workload_key,
                window_days=settings.incident_memory_recurring_window_days,
                threshold=settings.incident_memory_recurring_threshold,
            )
            patterns = [
                RecurringPattern(
                    alert_name=row["alert_name"],
                    root_cause=row["root_cause"],
                    occurrences=row["occurrences"],
                    first_seen=row.get("first_seen"),
                    last_seen=row.get("last_seen"),
                    fixes_tried=list(row.get("fixes_tried", [])),
                )
                for row in pattern_rows
            ]

        ctx = MemoryContext(
            similar_incidents=similar_incidents,
            fix_success_rates=fix_rates,
            recurring_patterns=patterns,
        )

        if ctx.has_data:
            logger.info(
                "Incident memory recall: %d similar, %d fix rates, %d recurring patterns (alert=%s)",
                len(similar_incidents), len(fix_rates), len(patterns), alert.alert_name,
            )

        return ctx

    def format_for_prompt(self, ctx: MemoryContext) -> str | None:
        """Format memory context into a system prompt section.

        Returns None if no relevant history exists (the section is omitted entirely).
        """
        if not ctx.has_data:
            return None

        sections: list[str] = ["## Incident Memory — Similar Past Incidents"]

        # Similar incidents
        if ctx.similar_incidents:
            sections.append(f"\n### Past Incidents ({len(ctx.similar_incidents)} matches)")
            for i, inc in enumerate(ctx.similar_incidents[:5], 1):
                date_str = inc.resolved_at.strftime("%Y-%m-%d") if inc.resolved_at else "unknown"
                outcome_emoji = {"success": "SUCCESS", "failed": "FAILED", "rejected": "REJECTED", "escalated": "ESCALATED"}.get(inc.outcome, inc.outcome.upper())
                sim_str = f" (similarity: {inc.similarity:.2f})" if inc.similarity > 0 else ""
                sections.append(
                    f"{i}. [{date_str}] {inc.alert_name} in {inc.namespace}\n"
                    f"   Root cause: {inc.root_cause}\n"
                    f"   Fix: {inc.fix_summary} -> {outcome_emoji}{sim_str}"
                )

        # Fix success rates
        if ctx.fix_success_rates:
            sections.append("\n### Fix Success Rates")
            for rate in ctx.fix_success_rates[:5]:
                sections.append(f'- "{rate.fix_summary}": {rate.rate_str}')

        # Recurring patterns
        if ctx.recurring_patterns:
            sections.append("\n### Recurring Pattern Warning")
            for pattern in ctx.recurring_patterns:
                days = settings.incident_memory_recurring_window_days
                fixes = ", ".join(f'"{f}"' for f in pattern.fixes_tried[:3])
                sections.append(
                    f"This workload has had {pattern.occurrences} "
                    f'"{pattern.alert_name}" incidents in the last {days} days.\n'
                    f"Common root cause: {pattern.root_cause}\n"
                    f"Fixes tried: {fixes}\n"
                    f"Consider recommending a permanent architectural fix rather than "
                    f"the same remediation."
                )

        return "\n".join(sections)

    def _determine_outcome(self, session: DiagnosisSession) -> str:
        """Map session phase to an outcome string."""
        from .session import SessionPhase

        phase = session.phase
        if phase == SessionPhase.RESOLVED:
            if session.approval.executed:
                return "success"
            elif session.approval.status.value == "rejected":
                return "rejected"
            return "success"
        elif phase == SessionPhase.ESCALATED:
            return "escalated"
        elif phase == SessionPhase.FAILED:
            return "failed"
        return "unknown"


# Module-level singleton
incident_memory = IncidentMemory()
