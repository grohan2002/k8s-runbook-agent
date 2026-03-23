"""PostgreSQL session persistence layer.

Uses asyncpg for async connection pooling. Stores sessions as JSONB
so the schema stays flexible as the session model evolves.

Tables:
  - sessions: one row per DiagnosisSession, JSONB payload + indexed columns
  - audit_log: append-only log of all state transitions and actions

Requires:
  DATABASE_URL env var (e.g. postgresql://user:pass@host:5432/k8s_agent)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool (module-level singleton, initialized at startup)
# ---------------------------------------------------------------------------
_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    """Create the connection pool and ensure tables exist.

    Called once during FastAPI lifespan startup.
    """
    global _pool
    if not settings.database_url:
        logger.warning("DATABASE_URL not set — session persistence disabled (in-memory only)")
        return

    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=10,
        command_timeout=10,
    )

    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)

        # Create incident memory table (requires pgvector extension)
        try:
            await conn.execute(_MEMORY_SCHEMA_SQL)
            logger.info("PostgreSQL incident memory table initialized (pgvector)")
        except Exception as e:
            logger.warning(
                "Could not create incident_memory table (pgvector may not be installed): %s",
                e,
            )

    logger.info("PostgreSQL session store initialized")


async def close_db() -> None:
    """Close the connection pool. Called during shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL connection pool closed")


def get_pool() -> asyncpg.Pool | None:
    return _pool


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    alert_name      TEXT NOT NULL,
    namespace       TEXT NOT NULL DEFAULT 'default',
    phase           TEXT NOT NULL DEFAULT 'alert_received',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Indexed columns for common queries
    fingerprint     TEXT,
    severity        TEXT,
    resolved        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_sessions_phase
    ON sessions (phase) WHERE NOT resolved;

CREATE INDEX IF NOT EXISTS idx_sessions_fingerprint
    ON sessions (fingerprint) WHERE NOT resolved;

CREATE INDEX IF NOT EXISTS idx_sessions_created
    ON sessions (created_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL DEFAULT 'system',
    old_phase       TEXT,
    new_phase       TEXT,
    details         JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_session
    ON audit_log (session_id, created_at DESC);
"""

# Incident memory table — created separately since it needs pgvector extension
_MEMORY_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incident_memory (
    id                BIGSERIAL PRIMARY KEY,
    session_id        TEXT NOT NULL,

    -- Alert pattern
    alert_name        TEXT NOT NULL,
    namespace         TEXT NOT NULL,
    workload_key      TEXT,

    -- Diagnosis
    root_cause        TEXT NOT NULL,
    confidence        TEXT NOT NULL,
    evidence          JSONB DEFAULT '[]'::jsonb,

    -- Fix
    fix_summary       TEXT NOT NULL,
    fix_description   TEXT DEFAULT '',
    fix_risk_level    TEXT DEFAULT 'medium',
    runbook_id        TEXT,

    -- Outcome
    outcome           TEXT NOT NULL,
    execution_result  TEXT,

    -- Vector embedding (voyage-3: 1024 dims)
    embedding         vector(1024),

    -- Full-text search fallback
    search_vector     tsvector,

    -- Timestamps
    resolved_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_embedding
    ON incident_memory USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memory_search
    ON incident_memory USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_memory_workload
    ON incident_memory (workload_key, resolved_at DESC)
    WHERE workload_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_alert
    ON incident_memory (alert_name, resolved_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_outcome
    ON incident_memory (alert_name, fix_summary, outcome);
"""


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------
async def save_session(session: "DiagnosisSession") -> None:
    """Upsert a session to PostgreSQL. Serializes the full session as JSONB."""
    pool = get_pool()
    if not pool:
        return  # No DB configured — silently skip

    payload = _serialize_session(session)
    resolved = session.phase.value in ("resolved", "failed", "escalated")

    await pool.execute(
        """
        INSERT INTO sessions (id, alert_name, namespace, phase, created_at, updated_at,
                              payload, fingerprint, severity, resolved)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
        ON CONFLICT (id) DO UPDATE SET
            phase = EXCLUDED.phase,
            updated_at = EXCLUDED.updated_at,
            payload = EXCLUDED.payload,
            resolved = EXCLUDED.resolved
        """,
        session.id,
        session.alert.alert_name,
        session.alert.namespace,
        session.phase.value,
        session.created_at,
        session.updated_at,
        json.dumps(payload, default=str),
        session.alert.fingerprint,
        session.alert.severity,
        resolved,
    )


async def load_session(session_id: str) -> dict[str, Any] | None:
    """Load a session payload from PostgreSQL. Returns the JSONB payload dict."""
    pool = get_pool()
    if not pool:
        return None

    row = await pool.fetchrow(
        "SELECT payload FROM sessions WHERE id = $1", session_id
    )
    if row is None:
        return None

    return json.loads(row["payload"])


async def load_active_sessions() -> list[dict[str, Any]]:
    """Load all non-resolved sessions."""
    pool = get_pool()
    if not pool:
        return []

    rows = await pool.fetch(
        "SELECT payload FROM sessions WHERE NOT resolved ORDER BY created_at DESC"
    )
    return [json.loads(row["payload"]) for row in rows]


async def load_recent_sessions(limit: int = 50) -> list[dict[str, Any]]:
    """Load the most recent sessions."""
    pool = get_pool()
    if not pool:
        return []

    rows = await pool.fetch(
        "SELECT payload FROM sessions ORDER BY created_at DESC LIMIT $1", limit
    )
    return [json.loads(row["payload"]) for row in rows]


async def find_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    """Find an active session for a deduplicated alert fingerprint."""
    pool = get_pool()
    if not pool:
        return None

    row = await pool.fetchrow(
        "SELECT payload FROM sessions WHERE fingerprint = $1 AND NOT resolved "
        "ORDER BY created_at DESC LIMIT 1",
        fingerprint,
    )
    if row is None:
        return None

    return json.loads(row["payload"])


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
async def write_audit_log(
    session_id: str,
    event_type: str,
    actor: str = "system",
    old_phase: str | None = None,
    new_phase: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append an entry to the audit log."""
    pool = get_pool()
    if not pool:
        return

    await pool.execute(
        """
        INSERT INTO audit_log (session_id, event_type, actor, old_phase, new_phase, details)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        session_id,
        event_type,
        actor,
        old_phase,
        new_phase,
        json.dumps(details or {}, default=str),
    )


async def get_audit_log(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Get audit log entries for a session."""
    pool = get_pool()
    if not pool:
        return []

    rows = await pool.fetch(
        "SELECT event_type, actor, old_phase, new_phase, details, created_at "
        "FROM audit_log WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",
        session_id,
        limit,
    )
    return [
        {
            "event_type": row["event_type"],
            "actor": row["actor"],
            "old_phase": row["old_phase"],
            "new_phase": row["new_phase"],
            "details": json.loads(row["details"]) if row["details"] else {},
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def _serialize_session(session: "DiagnosisSession") -> dict[str, Any]:
    """Convert a DiagnosisSession to a JSON-serializable dict.

    We store the full state so sessions survive process restarts.
    Conversation messages are excluded (too large) — they live only in memory.
    """
    from .models import ApprovalStatus

    data: dict[str, Any] = {
        "id": session.id,
        "phase": session.phase.value,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "alert": session.alert.model_dump(mode="json"),
        "tool_calls_made": session.tool_calls_made,
        "total_tokens_used": session.total_tokens_used,
        "error": session.error,
        "slack_thread_ts": session.slack_thread_ts,
        "slack_channel": session.slack_channel,
    }

    if session.diagnosis:
        data["diagnosis"] = session.diagnosis.model_dump(mode="json")

    if session.fix_proposal:
        data["fix_proposal"] = session.fix_proposal.model_dump(mode="json")

    data["approval"] = {
        "status": session.approval.status.value,
        "approved_by": session.approval.approved_by,
        "approved_at": session.approval.approved_at.isoformat() if session.approval.approved_at else None,
        "executed": session.approval.executed,
        "execution_result": session.approval.execution_result,
    }

    if session.runbook:
        data["runbook_id"] = session.runbook.metadata.id

    return data


# ---------------------------------------------------------------------------
# Incident memory CRUD
# ---------------------------------------------------------------------------
async def save_incident_memory(
    session_id: str,
    alert_name: str,
    namespace: str,
    workload_key: str | None,
    root_cause: str,
    confidence: str,
    evidence: list[str],
    fix_summary: str,
    fix_description: str,
    fix_risk_level: str,
    runbook_id: str | None,
    outcome: str,
    execution_result: str | None,
    embedding: list[float] | None = None,
    resolved_at: datetime | None = None,
) -> None:
    """Persist an incident to the memory table with optional vector embedding."""
    pool = get_pool()
    if not pool:
        return

    # Build tsvector for full-text search fallback
    search_text = f"{alert_name} {namespace} {root_cause} {fix_summary}"

    try:
        if embedding:
            # pgvector needs the vector as a string: '[0.1,0.2,...]'
            vec_str = "[" + ",".join(str(f) for f in embedding) + "]"
            await pool.execute(
                """
                INSERT INTO incident_memory
                    (session_id, alert_name, namespace, workload_key, root_cause,
                     confidence, evidence, fix_summary, fix_description, fix_risk_level,
                     runbook_id, outcome, execution_result, embedding, search_vector, resolved_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13,
                        $14::vector, to_tsvector('english', $15), $16)
                """,
                session_id, alert_name, namespace, workload_key, root_cause,
                confidence, json.dumps(evidence), fix_summary, fix_description,
                fix_risk_level, runbook_id, outcome, execution_result,
                vec_str, search_text, resolved_at or datetime.now(timezone.utc),
            )
        else:
            await pool.execute(
                """
                INSERT INTO incident_memory
                    (session_id, alert_name, namespace, workload_key, root_cause,
                     confidence, evidence, fix_summary, fix_description, fix_risk_level,
                     runbook_id, outcome, execution_result, search_vector, resolved_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13,
                        to_tsvector('english', $14), $15)
                """,
                session_id, alert_name, namespace, workload_key, root_cause,
                confidence, json.dumps(evidence), fix_summary, fix_description,
                fix_risk_level, runbook_id, outcome, execution_result,
                search_text, resolved_at or datetime.now(timezone.utc),
            )
    except Exception:
        logger.exception("Failed to save incident memory for session %s", session_id)


async def find_similar_incidents_by_vector(
    embedding: list[float],
    limit: int = 5,
    min_similarity: float = 0.7,
) -> list[dict[str, Any]]:
    """Find similar past incidents using vector cosine similarity."""
    pool = get_pool()
    if not pool:
        return []

    vec_str = "[" + ",".join(str(f) for f in embedding) + "]"

    try:
        rows = await pool.fetch(
            """
            SELECT session_id, alert_name, namespace, workload_key, root_cause,
                   confidence, fix_summary, fix_risk_level, outcome,
                   execution_result, resolved_at,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM incident_memory
            WHERE embedding IS NOT NULL
              AND 1 - (embedding <=> $1::vector) > $2
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            vec_str, min_similarity, limit,
        )
        return [dict(row) for row in rows]
    except Exception:
        logger.exception("Vector similarity search failed")
        return []


async def find_similar_incidents_by_text(
    alert_name: str,
    workload_key: str | None,
    search_query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Fallback: find similar incidents using structural match + tsvector."""
    pool = get_pool()
    if not pool:
        return []

    try:
        rows = await pool.fetch(
            """
            SELECT session_id, alert_name, namespace, workload_key, root_cause,
                   confidence, fix_summary, fix_risk_level, outcome,
                   execution_result, resolved_at,
                   CASE WHEN alert_name = $1 AND workload_key = $2 THEN 0.95
                        WHEN alert_name = $1 THEN 0.85
                        WHEN workload_key = $2 THEN 0.75
                        ELSE ts_rank_cd(search_vector, plainto_tsquery('english', $3)) * 0.7
                   END AS similarity
            FROM incident_memory
            WHERE alert_name = $1
               OR (workload_key = $2 AND $2 IS NOT NULL)
               OR search_vector @@ plainto_tsquery('english', $3)
            ORDER BY similarity DESC, resolved_at DESC
            LIMIT $4
            """,
            alert_name, workload_key, search_query, limit,
        )
        return [dict(row) for row in rows]
    except Exception:
        logger.exception("Text similarity search failed")
        return []


async def get_fix_success_rates(alert_name: str, limit: int = 5) -> list[dict[str, Any]]:
    """Get success/failure counts grouped by fix_summary for an alert type."""
    pool = get_pool()
    if not pool:
        return []

    try:
        rows = await pool.fetch(
            """
            SELECT fix_summary,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE outcome = 'success') AS successes,
                   COUNT(*) FILTER (WHERE outcome = 'failed') AS failures,
                   COUNT(*) FILTER (WHERE outcome = 'rejected') AS rejections
            FROM incident_memory
            WHERE alert_name = $1
            GROUP BY fix_summary
            ORDER BY total DESC
            LIMIT $2
            """,
            alert_name, limit,
        )
        return [dict(row) for row in rows]
    except Exception:
        logger.exception("Fix success rate query failed")
        return []


async def get_recurring_patterns(
    workload_key: str,
    window_days: int = 7,
    threshold: int = 3,
) -> list[dict[str, Any]]:
    """Detect recurring alert patterns on the same workload."""
    pool = get_pool()
    if not pool:
        return []

    try:
        rows = await pool.fetch(
            """
            SELECT alert_name, root_cause,
                   COUNT(*) AS occurrences,
                   MIN(resolved_at) AS first_seen,
                   MAX(resolved_at) AS last_seen,
                   array_agg(DISTINCT fix_summary) AS fixes_tried
            FROM incident_memory
            WHERE workload_key = $1
              AND resolved_at > now() - ($2 || ' days')::interval
            GROUP BY alert_name, root_cause
            HAVING COUNT(*) >= $3
            ORDER BY occurrences DESC
            """,
            workload_key, str(window_days), threshold,
        )
        return [dict(row) for row in rows]
    except Exception:
        logger.exception("Recurring pattern query failed")
        return []


# Type import at bottom to avoid circular import
from .agent.session import DiagnosisSession  # noqa: E402
