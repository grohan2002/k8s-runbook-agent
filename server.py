"""FastAPI application — ties together webhooks, Slack, and health endpoints.

Run with:
    uvicorn k8s_runbook_agent.server:app --host 0.0.0.0 --port 8080

Endpoints:
    POST /webhooks/grafana          — Grafana alert webhook
    POST /webhooks/grafana/resolved — Grafana resolved alert webhook
    POST /slack/interactions        — Slack interactive button callbacks
    POST /slack/commands            — /k8s-diag slash command
    GET  /health                    — Liveness probe
    GET  /ready                     — Deep readiness probe (K8s, PG, Slack)
    GET  /metrics                   — Prometheus metrics
    GET  /sessions                  — List active diagnosis sessions (debug)
    GET  /sessions/{id}/audit       — Audit log for a session
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .agent.session import session_store
from .config import settings
from .observability.metrics import active_sessions, collect_metrics
from .observability.rate_limit import debug_limiter
from .slack.bot import router as slack_router
from .webhooks.grafana import router as grafana_router

# ---------------------------------------------------------------------------
# Logging — structured JSON in production
# ---------------------------------------------------------------------------
from .observability.logging import configure_logging

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown hooks."""
    logger.info("K8s Runbook Agent starting...")
    logger.info("  Dry-run default: %s", settings.dry_run_default)
    logger.info("  Runbook dir: %s", settings.runbook_dir)
    logger.info("  Log level: %s", settings.log_level)

    # Production mode validation
    from .security import PRODUCTION_MODE, validate_production_config

    config_warnings = validate_production_config()
    for w in config_warnings:
        if PRODUCTION_MODE:
            logger.error("  PRODUCTION: %s", w)
        else:
            logger.warning("  %s", w)

    if PRODUCTION_MODE and config_warnings:
        critical = [w for w in config_warnings if "ANTHROPIC_API_KEY" in w]
        if critical:
            raise RuntimeError(f"Cannot start in production mode: {critical[0]}")

    # Verify critical config
    warnings = []
    if not settings.anthropic_api_key:
        warnings.append("ANTHROPIC_API_KEY not set — agent cannot diagnose")
    if not settings.slack_bot_token:
        warnings.append("SLACK_BOT_TOKEN not set — Slack notifications disabled")
    if not settings.slack_channel_id:
        warnings.append("SLACK_CHANNEL_ID not set — no default channel for alerts")
    if not settings.grafana_webhook_secret:
        warnings.append("GRAFANA_WEBHOOK_SECRET not set — webhook auth disabled (dev mode)")

    for w in warnings:
        logger.warning("  %s", w)

    # Initialize OpenTelemetry tracing (no-op if not configured)
    from .observability.tracing import init_tracing

    init_tracing()

    # Initialize PostgreSQL connection pool
    from .db import close_db, init_db

    await init_db()
    if settings.database_url:
        logger.info("  PostgreSQL: connected")
    else:
        logger.warning("  DATABASE_URL not set — sessions are in-memory only")

    # Pre-load runbooks
    from .knowledge.loader import RunbookStore

    store = RunbookStore()
    count = store.load_directory(settings.runbook_dir)
    logger.info("  Loaded %d diagnostic runbooks", count)

    # Initialize RBAC policy
    from .agent.rbac import approval_policy

    approval_policy.configure(
        allowed_users=settings.approval_allowed_users,
        allowed_groups=settings.approval_allowed_groups,
        senior_users=settings.approval_senior_users,
        min_risk_for_senior=settings.approval_min_risk_for_senior,
    )

    # Initialize multi-cluster support
    from .agent.multi_cluster import cluster_registry

    if settings.cluster_configs:
        cluster_count = cluster_registry.load_from_json(settings.cluster_configs)
        logger.info("  Multi-cluster: %d clusters registered", cluster_count)
    else:
        logger.info("  Multi-cluster: disabled (single-cluster mode)")

    # Register incident management providers (PagerDuty, OpsGenie)
    from .notifications.base import IncidentContext, incident_router
    from .notifications.opsgenie import OpsGenieProvider
    from .notifications.pagerduty import PagerDutyProvider

    incident_router.register(PagerDutyProvider())
    incident_router.register(OpsGenieProvider())

    if incident_router.has_providers:
        logger.info("  Incident providers: %s", ", ".join(incident_router.enabled_providers))
    else:
        logger.info("  Incident providers: none configured (Slack-only escalation)")

    # Start escalation timer
    from .agent.escalation import EscalationConfig, EscalationTimer
    from .slack.bot import post_in_thread

    async def _on_escalate(session, message):
        # 1. Always post to Slack
        await post_in_thread(
            session,
            {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message}}]},
            "Escalation",
        )

        # 2. Create incident on PagerDuty / OpsGenie (if configured)
        if incident_router.has_providers:
            ctx = IncidentContext.from_session(session, message)
            incident_ids = await incident_router.create_incident(ctx)
            if incident_ids:
                # Store incident IDs on the session for later ack/resolve
                if not hasattr(session, "incident_ids"):
                    session.incident_ids = {}
                session.incident_ids.update(incident_ids)
                logger.info(
                    "Session %s: incidents created: %s", session.id, incident_ids,
                )

    esc_config = EscalationConfig(
        sla_seconds={
            "critical": settings.escalation_sla_critical,
            "warning": settings.escalation_sla_warning,
            "info": settings.escalation_sla_info,
        },
        escalation_group=settings.escalation_group,
    )
    esc_timer = EscalationTimer(config=esc_config, on_escalate=_on_escalate)
    esc_task = asyncio.create_task(esc_timer.run())
    logger.info("  Escalation timer: started (SLAs: %s)", esc_config.sla_seconds)

    # Start runbook hot-reload watcher
    from .knowledge.hot_reload import RunbookWatcher

    watcher = RunbookWatcher(store, settings.runbook_dir, settings.runbook_poll_interval)
    await watcher.start()
    watcher_task = asyncio.create_task(watcher.run())
    logger.info("  Runbook hot-reload: watching %s (every %ds)", settings.runbook_dir, settings.runbook_poll_interval)

    # Start secret reloader
    from .agent.secret_reload import SecretReloader

    secret_reloader = SecretReloader()
    await secret_reloader.start()
    secret_reload_task = asyncio.create_task(secret_reloader.run())
    app.state.secret_reloader = secret_reloader
    if secret_reloader.enabled:
        logger.info("  Secret reloader: watching %s", secret_reloader.secret_dir)

    # Start data retention manager (prunes old sessions, audit logs, memory)
    from .agent.retention import retention_manager

    retention_task = asyncio.create_task(retention_manager.run())
    from .agent.retention import SESSION_RETENTION_DAYS, AUDIT_RETENTION_DAYS, MEMORY_RETENTION_DAYS

    logger.info("  Retention manager: started (sessions=%dd, audit=%dd, memory=%dd)",
                SESSION_RETENTION_DAYS, AUDIT_RETENTION_DAYS, MEMORY_RETENTION_DAYS)

    yield

    # Shutdown
    esc_timer.stop()
    esc_task.cancel()
    watcher.stop()
    watcher_task.cancel()
    secret_reload_task.cancel()
    retention_task.cancel()

    for task in (esc_task, watcher_task, secret_reload_task, retention_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    await close_db()
    logger.info("K8s Runbook Agent shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="K8s Runbook Automation Agent",
    description="AI-powered Kubernetes troubleshooting agent with Grafana + Slack integration",
    version="0.3.0",
    lifespan=lifespan,
)

# Mount routers
app.include_router(grafana_router)
app.include_router(slack_router)


# ---------------------------------------------------------------------------
# Security middleware (payload limits, security headers)
# ---------------------------------------------------------------------------
from .security import install_security_middleware

install_security_middleware(app)


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://*.slack.com",
        "https://*.grafana.com",
        "https://*.grafana.net",
    ],
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
)


# ---------------------------------------------------------------------------
# Request ID + logging middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next) -> Response:
    """Add request ID and log every request with method, path, status, and duration."""
    import uuid as _uuid

    # Propagate or generate request ID
    request_id = request.headers.get("X-Request-ID", _uuid.uuid4().hex[:16])
    request.state.request_id = request_id

    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000

    # Attach request ID to response
    response.headers["X-Request-ID"] = request_id

    # Skip noisy health/metrics endpoints
    if request.url.path not in ("/health", "/metrics"):
        logger.info(
            "%s %s → %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "method": request.method,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 1),
                "request_id": request_id,
            },
        )

    return response


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — returns 200 if the process is running."""
    return {"status": "ok"}


@app.get("/ready")
async def readiness() -> JSONResponse:
    """Deep readiness probe — checks all dependencies with actual connectivity tests."""
    checks: dict[str, Any] = {}

    # 1. Anthropic API key present
    checks["anthropic_api_key"] = "ok" if settings.anthropic_api_key else "missing"

    # 2. Kubernetes connectivity — try listing namespaces
    try:
        from .k8s_client import _k8s_configured, core_v1

        if _k8s_configured:
            # Actual API call to verify connectivity (with 5s timeout)
            try:
                await asyncio.wait_for(
                    core_v1.list_namespace(limit=1),
                    timeout=5.0,
                )
                checks["kubernetes"] = "ok"
            except asyncio.TimeoutError:
                checks["kubernetes"] = "timeout"
            except Exception as e:
                checks["kubernetes"] = f"error: {type(e).__name__}"
        else:
            checks["kubernetes"] = "no_config"
    except Exception as e:
        checks["kubernetes"] = f"import_error: {e}"

    # 3. PostgreSQL connectivity — try a simple query
    from .db import get_pool

    pool = get_pool()
    if pool:
        try:
            async with pool.acquire(timeout=5.0) as conn:
                await conn.fetchval("SELECT 1")
            checks["postgresql"] = "ok"
        except Exception as e:
            checks["postgresql"] = f"error: {type(e).__name__}"
    else:
        checks["postgresql"] = "not_configured"

    # 4. Slack token present
    checks["slack_token"] = "ok" if settings.slack_bot_token else "missing"

    # 5. Embedding provider (Voyage AI) health check
    try:
        from .agent.embeddings import embedding_provider

        embed_health = await embedding_provider.health_check()
        checks["embeddings"] = embed_health.get("status", "unknown")
        if embed_health.get("status") == "ok":
            checks["embeddings_model"] = embed_health.get("model", "")
            checks["embeddings_latency_ms"] = embed_health.get("latency_ms", 0)
    except Exception as e:
        checks["embeddings"] = f"error: {type(e).__name__}"

    # 6. Active session count (gauge update)
    active_count = len(session_store.active_sessions())
    active_sessions.set(active_count)
    checks["active_sessions"] = active_count

    # 7. Multi-agent system health (summary only — full details at /ready/agents)
    if settings.multi_agent_enabled:
        try:
            from .agent.multi_agent.health import agent_health_checker, summarize_health

            agent_results = await agent_health_checker.check_all()
            summary = summarize_health(agent_results)
            checks["multi_agent"] = summary["status"]
            checks["multi_agent_healthy"] = summary["healthy_count"]
            checks["multi_agent_errors"] = summary["error_count"]
        except Exception as e:
            checks["multi_agent"] = f"error: {type(e).__name__}"
    else:
        checks["multi_agent"] = "not_configured"

    # Determine overall status
    critical_checks = ["anthropic_api_key", "kubernetes"]
    critical_ok = all(checks.get(c) == "ok" for c in critical_checks)
    all_ok = all(
        v in ("ok", "not_configured", "all_healthy")
        or isinstance(v, (int, float))
        for v in checks.values()
    )

    if critical_ok and all_ok:
        status = "ready"
        status_code = 200
    elif critical_ok:
        status = "degraded"
        status_code = 200
    else:
        status = "not_ready"
        status_code = 503

    return JSONResponse(
        status_code=status_code,
        content={"status": status, "checks": checks},
    )


@app.get("/ready/agents")
async def agent_readiness(force: bool = False) -> JSONResponse:
    """Per-agent health check — detailed status of every agent in the system.

    Query params:
        force=true — bypass 60s cache and re-probe all agents

    Returns individual health for: triage, 4 specialists, coordinator, executor, embeddings.
    """
    from .agent.multi_agent.health import agent_health_checker, summarize_health

    results = await agent_health_checker.check_all(force=force)
    summary = summarize_health(results)

    status_code = 200 if summary["status"] != "unhealthy" else 503
    return JSONResponse(status_code=status_code, content=summary)


# ---------------------------------------------------------------------------
# Prometheus metrics endpoint
# ---------------------------------------------------------------------------
@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    """Prometheus-compatible metrics endpoint."""
    # Update active sessions gauge before collecting
    active_sessions.set(len(session_store.active_sessions()))
    return PlainTextResponse(
        content=collect_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Debug endpoints
# ---------------------------------------------------------------------------
@app.get("/sessions")
async def list_sessions(request: Request) -> JSONResponse:
    """Debug endpoint — list all diagnosis sessions."""
    if not debug_limiter.allow("sessions"):
        return JSONResponse(status_code=429, content={"error": "Rate limit exceeded"})

    sessions = session_store.all_sessions()
    return JSONResponse(content={
        "total": len(sessions),
        "active": len(session_store.active_sessions()),
        "sessions": [
            {
                "id": s.id,
                "alert": s.alert.alert_name,
                "namespace": s.alert.namespace,
                "pod": s.alert.pod,
                "phase": s.phase.value,
                "tool_calls": s.tool_calls_made,
                "tokens": s.total_tokens_used,
                "created": s.created_at.isoformat(),
                "has_diagnosis": s.diagnosis is not None,
                "has_fix": s.fix_proposal is not None,
                "error": s.error,
            }
            for s in sorted(sessions, key=lambda x: x.created_at, reverse=True)
        ],
    })


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    """Debug endpoint — get full details for a session."""
    if not debug_limiter.allow(f"session:{session_id}"):
        return JSONResponse(status_code=429, content={"error": "Rate limit exceeded"})

    session = session_store.get(session_id)
    if not session:
        return JSONResponse(status_code=404, content={"error": f"Session {session_id} not found"})

    result: dict = {
        "id": session.id,
        "alert": {
            "name": session.alert.alert_name,
            "namespace": session.alert.namespace,
            "pod": session.alert.pod,
            "severity": session.alert.severity,
            "labels": session.alert.labels,
        },
        "phase": session.phase.value,
        "tool_calls": session.tool_calls_made,
        "tokens": session.total_tokens_used,
        "created": session.created_at.isoformat(),
        "updated": session.updated_at.isoformat(),
    }

    if session.diagnosis:
        result["diagnosis"] = {
            "root_cause": session.diagnosis.root_cause,
            "confidence": session.diagnosis.confidence.value,
            "evidence": session.diagnosis.evidence,
            "ruled_out": session.diagnosis.ruled_out,
        }

    if session.fix_proposal:
        result["fix_proposal"] = {
            "summary": session.fix_proposal.summary,
            "risk_level": session.fix_proposal.risk_level.value,
            "description": session.fix_proposal.description,
            "dry_run": session.fix_proposal.dry_run_output,
            "rollback": session.fix_proposal.rollback_plan,
            "needs_human_input": session.fix_proposal.requires_human_values,
            "human_fields": session.fix_proposal.human_value_fields,
        }

    if session.error:
        result["error"] = session.error

    return JSONResponse(content=result)


@app.get("/sessions/{session_id}/audit")
async def get_session_audit(session_id: str) -> JSONResponse:
    """Get the audit log for a session — every state transition and action."""
    if not debug_limiter.allow(f"audit:{session_id}"):
        return JSONResponse(status_code=429, content={"error": "Rate limit exceeded"})

    from .db import get_audit_log

    entries = await get_audit_log(session_id)
    return JSONResponse(content={
        "session_id": session_id,
        "entries": entries,
    })


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------
@app.post("/admin/runbooks/reload")
async def reload_runbooks(request: Request) -> JSONResponse:
    """Trigger a manual runbook reload. Requires admin auth in production."""
    from .security import sanitize_error, verify_admin_auth

    verify_admin_auth(request)

    from .knowledge.loader import RunbookStore

    store = RunbookStore()
    try:
        count = store.load_directory(settings.runbook_dir)
        return JSONResponse(content={"status": "ok", "runbooks_loaded": count})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "error": sanitize_error(e)})


@app.post("/admin/secrets/reload")
async def reload_secrets(request: Request) -> JSONResponse:
    """Force-reload secrets from env or mounted files. Requires admin auth."""
    from .security import verify_admin_auth

    verify_admin_auth(request)

    reloader = getattr(app.state, "secret_reloader", None)
    if reloader:
        changed = reloader.force_reload()
        return JSONResponse(content={
            "reloaded": len(changed),
            "fields": list(changed.keys()),
            "status": reloader.status(),
        })
    from .agent.secret_reload import reload_secrets_from_env
    changed = reload_secrets_from_env()
    return JSONResponse(content={"reloaded": len(changed), "fields": list(changed.keys())})


@app.get("/admin/clusters")
async def list_clusters(request: Request) -> JSONResponse:
    """Show registered clusters (multi-cluster mode). Requires admin auth."""
    from .security import verify_admin_auth

    verify_admin_auth(request)

    from .agent.multi_cluster import cluster_registry

    return JSONResponse(content={
        "multi_cluster": cluster_registry.is_multi_cluster,
        "default_cluster": cluster_registry.default_cluster,
        "clusters": cluster_registry.cluster_names,
        "summary": cluster_registry.summary(),
    })
