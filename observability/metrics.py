"""Prometheus metrics for the K8s Runbook Agent.

Exports metrics at /metrics in Prometheus exposition format.
No external dependency — uses a minimal custom implementation
to avoid pulling in prometheus_client and its multiprocess complexity.

Metrics exported:
  Counters:
    - runbook_alerts_received_total{alert_name, severity, namespace}
    - runbook_diagnoses_total{confidence, alert_name}
    - runbook_fixes_executed_total{result, risk_level}
    - runbook_escalations_total{reason_category}
    - runbook_anthropic_calls_total{model, status}
    - runbook_tool_calls_total{tool_name, status}
  Gauges:
    - runbook_active_sessions
  Histograms:
    - runbook_diagnosis_duration_seconds
    - runbook_execution_duration_seconds
    - runbook_anthropic_call_duration_seconds
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


class _Counter:
    """Thread-safe counter with labels."""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        key = tuple(sorted((labels or {}).items()))
        with self._lock:
            self._values[key] += value

    def collect(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            for label_pairs, value in sorted(self._values.items()):
                label_str = ",".join(f'{k}="{v}"' for k, v in label_pairs)
                suffix = f"{{{label_str}}}" if label_str else ""
                lines.append(f"{self.name}{suffix} {value}")
        return lines


class _Gauge:
    """Thread-safe gauge."""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._value: float = 0.0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = value

    def inc(self, value: float = 1.0) -> None:
        with self._lock:
            self._value += value

    def dec(self, value: float = 1.0) -> None:
        with self._lock:
            self._value -= value

    def collect(self) -> list[str]:
        return [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} gauge",
            f"{self.name} {self._value}",
        ]


class _Histogram:
    """Thread-safe histogram with fixed buckets."""

    DEFAULT_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, float("inf"))

    def __init__(self, name: str, help_text: str, buckets: tuple[float, ...] | None = None) -> None:
        self.name = name
        self.help = help_text
        self._buckets = buckets or self.DEFAULT_BUCKETS
        self._counts: dict[float, int] = {b: 0 for b in self._buckets}
        self._sum: float = 0.0
        self._count: int = 0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1
            # Place in the smallest bucket that fits (non-cumulative per bucket)
            for b in self._buckets:
                if value <= b:
                    self._counts[b] += 1
                    break

    def collect(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        with self._lock:
            cumulative = 0
            for b in self._buckets:
                cumulative += self._counts[b]
                le = "+Inf" if b == float("inf") else str(b)
                lines.append(f'{self.name}_bucket{{le="{le}"}} {cumulative}')
            lines.append(f"{self.name}_sum {self._sum}")
            lines.append(f"{self.name}_count {self._count}")
        return lines


# ---------------------------------------------------------------------------
# Metric instances (module-level singletons)
# ---------------------------------------------------------------------------

alerts_received = _Counter(
    "runbook_alerts_received_total",
    "Total alerts received from Grafana",
)

diagnoses_completed = _Counter(
    "runbook_diagnoses_total",
    "Total diagnoses completed",
)

fixes_executed = _Counter(
    "runbook_fixes_executed_total",
    "Total fixes executed",
)

escalations = _Counter(
    "runbook_escalations_total",
    "Total escalations (agent could not resolve)",
)

anthropic_calls = _Counter(
    "runbook_anthropic_calls_total",
    "Total Anthropic API calls",
)

tool_calls = _Counter(
    "runbook_tool_calls_total",
    "Total tool calls made by the agent",
)

active_sessions = _Gauge(
    "runbook_active_sessions",
    "Number of currently active diagnosis sessions",
)

diagnosis_duration = _Histogram(
    "runbook_diagnosis_duration_seconds",
    "Time taken to complete a diagnosis",
)

execution_duration = _Histogram(
    "runbook_execution_duration_seconds",
    "Time taken to execute an approved fix",
)

anthropic_call_duration = _Histogram(
    "runbook_anthropic_call_duration_seconds",
    "Duration of individual Anthropic API calls",
)

tokens_used = _Counter(
    "runbook_tokens_used_total",
    "Total tokens consumed by Anthropic API calls",
)

# Multi-agent metrics
triage_routing = _Counter(
    "runbook_triage_routing_total",
    "Triage routing decisions by domain and confidence",
)

specialist_invocations = _Counter(
    "runbook_specialist_invocations_total",
    "Specialist agent invocations by domain",
)

coordinator_activations = _Counter(
    "runbook_coordinator_activations_total",
    "Coordinator agent activations for correlated alerts",
)

per_agent_tokens = _Counter(
    "runbook_per_agent_tokens_total",
    "Token usage broken down by agent type",
)


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
_ALL_METRICS = [
    alerts_received,
    diagnoses_completed,
    fixes_executed,
    escalations,
    anthropic_calls,
    tool_calls,
    active_sessions,
    diagnosis_duration,
    execution_duration,
    anthropic_call_duration,
    tokens_used,
    triage_routing,
    specialist_invocations,
    coordinator_activations,
    per_agent_tokens,
]


def collect_metrics() -> str:
    """Collect all metrics in Prometheus exposition format."""
    lines: list[str] = []
    for metric in _ALL_METRICS:
        lines.extend(metric.collect())
        lines.append("")  # Blank line between metrics
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Timer context manager
# ---------------------------------------------------------------------------
class Timer:
    """Context manager that records duration to a histogram.

    Usage:
        with Timer(diagnosis_duration):
            await do_investigation()
    """

    def __init__(self, histogram: _Histogram) -> None:
        self._histogram = histogram
        self._start: float = 0

    def __enter__(self) -> "Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: Any) -> None:
        duration = time.monotonic() - self._start
        self._histogram.observe(duration)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start
