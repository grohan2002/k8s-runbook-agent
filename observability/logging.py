"""Structured JSON logging with correlation IDs.

Every log line is a JSON object with:
  - timestamp, level, logger, message (standard)
  - session_id (correlation ID from contextvars)
  - alert_name, namespace (from session context when available)

The correlation ID flows automatically through async code via contextvars.
Set it once at the start of an investigation and every log line in that
call chain includes it — no manual passing needed.

Usage:
    from k8s_runbook_agent.observability.logging import set_session_context

    set_session_context(session_id="diag-abc123", alert_name="KubePodCrashLooping")
    logger.info("Starting investigation")
    # → {"timestamp": "...", "session_id": "diag-abc123", "alert_name": "...", ...}
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Context variables — flow through async code automatically
# ---------------------------------------------------------------------------
_session_id_var: ContextVar[str] = ContextVar("session_id", default="")
_alert_name_var: ContextVar[str] = ContextVar("alert_name", default="")
_namespace_var: ContextVar[str] = ContextVar("namespace", default="")


def set_session_context(
    session_id: str = "",
    alert_name: str = "",
    namespace: str = "",
) -> None:
    """Set correlation context for the current async task."""
    if session_id:
        _session_id_var.set(session_id)
    if alert_name:
        _alert_name_var.set(alert_name)
    if namespace:
        _namespace_var.set(namespace)


def clear_session_context() -> None:
    """Clear correlation context."""
    _session_id_var.set("")
    _alert_name_var.set("")
    _namespace_var.set("")


# ---------------------------------------------------------------------------
# JSON Formatter
# ---------------------------------------------------------------------------
class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects.

    Includes correlation IDs from contextvars when available.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Correlation IDs from contextvars
        session_id = _session_id_var.get("")
        if session_id:
            log_entry["session_id"] = session_id

        alert_name = _alert_name_var.get("")
        if alert_name:
            log_entry["alert_name"] = alert_name

        namespace = _namespace_var.get("")
        if namespace:
            log_entry["namespace"] = namespace

        # Include exception info if present
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }

        # Include extra fields passed via logger.info("msg", extra={...})
        for key in ("extra_data", "duration_ms", "tool_name", "status_code", "method"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        return json.dumps(log_entry, default=str)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger with structured JSON output.

    Call once at application startup (in server.py lifespan).
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # JSON handler to stdout (for container log collection)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes.client.rest").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
