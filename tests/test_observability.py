"""Tests for observability: metrics, rate limiting, structured logging."""

import json
import logging
import time

import pytest

from k8s_runbook_agent.observability.logging import (
    JSONFormatter,
    clear_session_context,
    set_session_context,
)
from k8s_runbook_agent.observability.metrics import (
    Timer,
    _Counter,
    _Gauge,
    _Histogram,
    collect_metrics,
)
from k8s_runbook_agent.observability.rate_limit import RateLimiter


class TestJSONFormatter:
    def test_basic_output(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello world", args=(), exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "hello world"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test"
        assert "timestamp" in parsed

    def test_includes_correlation_ids(self):
        set_session_context(session_id="diag-test", alert_name="TestAlert", namespace="prod")
        try:
            formatter = JSONFormatter()
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="test", args=(), exc_info=None,
            )
            output = formatter.format(record)
            parsed = json.loads(output)
            assert parsed["session_id"] == "diag-test"
            assert parsed["alert_name"] == "TestAlert"
            assert parsed["namespace"] == "prod"
        finally:
            clear_session_context()

    def test_omits_empty_correlation_ids(self):
        clear_session_context()
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "session_id" not in parsed

    def test_exception_info(self):
        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname="", lineno=0,
                msg="failed", args=(), exc_info=sys.exc_info(),
            )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["exception"]["type"] == "ValueError"
        assert parsed["exception"]["message"] == "test error"


class TestCounter:
    def test_increment(self):
        c = _Counter("test_counter", "Test")
        c.inc({"method": "GET"})
        c.inc({"method": "GET"})
        c.inc({"method": "POST"})
        lines = c.collect()
        text = "\n".join(lines)
        assert 'test_counter{method="GET"} 2' in text
        assert 'test_counter{method="POST"} 1' in text

    def test_no_labels(self):
        c = _Counter("test_counter2", "Test")
        c.inc()
        lines = c.collect()
        assert "test_counter2 1.0" in lines


class TestGauge:
    def test_set_and_inc_dec(self):
        g = _Gauge("test_gauge", "Test")
        g.set(5)
        assert "test_gauge 5" in g.collect()

        g.inc(3)
        assert "test_gauge 8" in g.collect()

        g.dec(2)
        assert "test_gauge 6" in g.collect()


class TestHistogram:
    def test_observe(self):
        h = _Histogram("test_hist", "Test", buckets=(1.0, 5.0, 10.0, float("inf")))
        h.observe(0.5)
        h.observe(3.0)
        h.observe(7.0)
        lines = h.collect()
        text = "\n".join(lines)
        assert 'le="1.0"} 1' in text    # 0.5 in bucket 1.0
        assert 'le="5.0"} 2' in text  # cumulative: 1 + 1 (3.0)
        assert 'le="10.0"} 3' in text # cumulative: 2 + 1 (7.0)
        assert "test_hist_count 3" in text
        assert "test_hist_sum 10.5" in text


class TestTimer:
    def test_records_duration(self):
        h = _Histogram("timer_test", "Test")
        with Timer(h) as t:
            time.sleep(0.02)
        assert t.elapsed >= 0.02
        lines = h.collect()
        assert "timer_test_count 1" in "\n".join(lines)


class TestCollectMetrics:
    def test_produces_valid_output(self):
        output = collect_metrics()
        assert "# HELP" in output
        assert "# TYPE" in output
        assert "runbook_alerts_received_total" in output
        assert "runbook_active_sessions" in output


class TestRateLimiter:
    def test_allows_within_burst(self):
        limiter = RateLimiter(rate=100, burst=5)
        for _ in range(5):
            assert limiter.allow("test") is True

    def test_blocks_after_burst(self):
        limiter = RateLimiter(rate=100, burst=3)
        for _ in range(3):
            limiter.allow("test")
        assert limiter.allow("test") is False

    def test_per_key_isolation(self):
        limiter = RateLimiter(rate=100, burst=1)
        assert limiter.allow("a") is True
        assert limiter.allow("b") is True
        assert limiter.allow("a") is False  # a exhausted
        assert limiter.allow("b") is False  # b exhausted

    def test_refills_over_time(self):
        limiter = RateLimiter(rate=1000, burst=1)
        assert limiter.allow("test") is True
        assert limiter.allow("test") is False
        time.sleep(0.01)  # Wait for refill (1000/s = 1 per ms)
        assert limiter.allow("test") is True

    def test_cleanup_stale_buckets(self):
        limiter = RateLimiter(rate=100, burst=5, cleanup_interval=0.01)
        limiter.allow("temp")
        assert "temp" in limiter._buckets
        time.sleep(0.05)
        limiter.allow("trigger_cleanup")  # triggers cleanup
        # temp should be cleaned up since it's old
        # (cleanup checks for 2 * cleanup_interval = 0.02s)
