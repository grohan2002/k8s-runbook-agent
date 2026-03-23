"""Production security hardening middleware and utilities.

Provides:
  1. Admin API key authentication for /admin/* endpoints
  2. Request payload size limiting (default 1MB)
  3. Security headers (HSTS, CSP, X-Frame-Options, etc.)
  4. Production mode enforcement (require secrets when not in dev)
  5. Error response sanitization (no stack traces to clients)
  6. Concurrent session limiting
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

logger = logging.getLogger(__name__)

# Maximum request body size (bytes). 0 = unlimited.
MAX_PAYLOAD_BYTES = int(os.getenv("MAX_PAYLOAD_BYTES", str(1 * 1024 * 1024)))  # 1MB

# Admin API key for /admin/* endpoints. Empty = no auth required (dev mode).
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# Maximum concurrent active sessions before rejecting new alerts.
MAX_CONCURRENT_SESSIONS = int(os.getenv("MAX_CONCURRENT_SESSIONS", "50"))

# Production mode — when True, require all critical secrets to be set.
PRODUCTION_MODE = os.getenv("PRODUCTION_MODE", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Admin authentication
# ---------------------------------------------------------------------------
def verify_admin_auth(request: Request) -> None:
    """Verify admin API key for /admin/* endpoints.

    In dev mode (no ADMIN_API_KEY set), all requests are allowed.
    In production, requires Bearer token matching ADMIN_API_KEY.
    """
    if not ADMIN_API_KEY:
        return  # Dev mode — no auth

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Admin API key required")

    token = auth[7:]  # Strip "Bearer "
    if token != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin API key")


# ---------------------------------------------------------------------------
# Payload size middleware
# ---------------------------------------------------------------------------
class PayloadSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests with bodies exceeding MAX_PAYLOAD_BYTES."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_PAYLOAD_BYTES > 0:
            return JSONResponse(
                status_code=413,
                content={"error": f"Request body too large (max {MAX_PAYLOAD_BYTES} bytes)"},
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # Prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # XSS protection (legacy browsers)
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Content Security Policy (API only — no HTML)
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        # HSTS (only if behind TLS terminator)
        if PRODUCTION_MODE:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Prevent caching of API responses
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"

        return response


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------
def sanitize_error(error: Exception) -> str:
    """Return a safe error message for client responses.

    In production mode, internal details are stripped.
    In dev mode, the full error is returned for debugging.
    """
    if PRODUCTION_MODE:
        error_type = type(error).__name__
        # Allowlist of safe error types to expose
        safe_types = {"ValueError", "KeyError", "HTTPException", "ValidationError"}
        if error_type in safe_types:
            return f"{error_type}: {error}"
        return f"Internal server error ({error_type})"
    return str(error)


# ---------------------------------------------------------------------------
# Session limit check
# ---------------------------------------------------------------------------
def check_session_limit() -> None:
    """Raise if concurrent active sessions exceed MAX_CONCURRENT_SESSIONS."""
    from .agent.session import session_store

    active = len(session_store.active_sessions())
    if MAX_CONCURRENT_SESSIONS > 0 and active >= MAX_CONCURRENT_SESSIONS:
        raise HTTPException(
            status_code=429,
            detail=f"Too many active sessions ({active}/{MAX_CONCURRENT_SESSIONS}). Try again later.",
        )


# ---------------------------------------------------------------------------
# Production mode validation
# ---------------------------------------------------------------------------
def validate_production_config() -> list[str]:
    """Check that all required secrets are configured for production.

    Returns a list of warnings. In PRODUCTION_MODE, these become errors.
    """
    warnings: list[str] = []

    if not settings.anthropic_api_key:
        warnings.append("ANTHROPIC_API_KEY not set — agent cannot diagnose")
    if not settings.slack_signing_secret:
        warnings.append("SLACK_SIGNING_SECRET not set — Slack requests are unauthenticated")
    if not settings.grafana_webhook_secret:
        warnings.append("GRAFANA_WEBHOOK_SECRET not set — webhook is unauthenticated")
    if not ADMIN_API_KEY:
        warnings.append("ADMIN_API_KEY not set — admin endpoints are unauthenticated")
    if not settings.database_url:
        warnings.append("DATABASE_URL not set — using in-memory sessions (no persistence)")

    return warnings


# ---------------------------------------------------------------------------
# Install all middleware on app
# ---------------------------------------------------------------------------
def install_security_middleware(app: FastAPI) -> None:
    """Install all security middleware on the FastAPI app."""
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(PayloadSizeLimitMiddleware)
    logger.info("Security middleware installed (payload_limit=%d, production=%s)", MAX_PAYLOAD_BYTES, PRODUCTION_MODE)
