"""Token bucket rate limiter for webhook and Slack endpoints.

Protects the agent from:
  - Grafana alert storms (noisy alerts firing hundreds of times)
  - Slack interaction replay attacks
  - Accidental load from misconfigured alert rules

Each rate limit bucket is identified by a key (e.g., IP address,
alert fingerprint, or endpoint name).

Usage:
    limiter = RateLimiter(rate=10, burst=20)  # 10 req/s, burst of 20

    if not limiter.allow("grafana-webhook"):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    """A single token bucket."""
    tokens: float
    last_refill: float
    max_tokens: float
    refill_rate: float  # tokens per second

    def consume(self) -> bool:
        """Try to consume a token. Returns True if allowed."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    """Per-key token bucket rate limiter.

    Args:
        rate: Sustained requests per second allowed.
        burst: Maximum burst size (bucket capacity).
        cleanup_interval: Seconds between stale bucket cleanup.
    """

    def __init__(
        self,
        rate: float = 10.0,
        burst: int = 20,
        cleanup_interval: float = 60.0,
    ) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.monotonic()

    def allow(self, key: str = "default") -> bool:
        """Check if a request for the given key is allowed.

        Returns True if allowed, False if rate limited.
        """
        with self._lock:
            self._maybe_cleanup()

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(
                    tokens=self._burst,
                    last_refill=time.monotonic(),
                    max_tokens=self._burst,
                    refill_rate=self._rate,
                )
                self._buckets[key] = bucket

            return bucket.consume()

    def _maybe_cleanup(self) -> None:
        """Remove stale buckets periodically."""
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        self._last_cleanup = now
        stale_threshold = now - self._cleanup_interval * 2
        stale_keys = [
            k for k, b in self._buckets.items()
            if b.last_refill < stale_threshold
        ]
        for k in stale_keys:
            del self._buckets[k]


# ---------------------------------------------------------------------------
# Pre-configured limiters for each endpoint category
# ---------------------------------------------------------------------------

# Grafana webhooks: 30 alerts/sec sustained, burst of 60
# (covers noisy alertmanager during incident storms)
webhook_limiter = RateLimiter(rate=30.0, burst=60)

# Slack interactions: 10/sec sustained, burst of 20
# (humans clicking buttons — much lower volume)
slack_limiter = RateLimiter(rate=10.0, burst=20)

# API/debug endpoints: 5/sec sustained, burst of 10
debug_limiter = RateLimiter(rate=5.0, burst=10)
