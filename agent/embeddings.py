"""Embedding provider for incident memory vector search.

Uses the Voyage AI API (voyage-3, 1024 dims) with the existing Anthropic API key.
Voyage accepts Anthropic API keys via their partnership integration.

Fallback: if Voyage is unavailable or the API call fails, the memory system
falls back to PostgreSQL tsvector full-text search (degraded but functional).

Health check: `health_check()` sends a tiny test embedding to verify the API
is reachable. Integrated into the /ready endpoint.

Usage:
    from .embeddings import embedding_provider

    vec = await embedding_provider.embed("OOMKilled in production/api-server")
    ok = await embedding_provider.health_check()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)


class EmbeddingProvider:
    """Voyage AI embedding provider using the Anthropic API key.

    Falls back gracefully when:
    - voyageai package not installed
    - No API key configured
    - API call fails (timeout, rate limit, etc.)
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._available = False
        self._last_health_check: float = 0
        self._health_ok = False
        self._init_attempted = False

    def _lazy_init(self) -> None:
        """Initialize the Voyage client on first use."""
        if self._init_attempted:
            return
        self._init_attempted = True

        api_key = settings.anthropic_api_key
        if not api_key:
            logger.info("Embeddings: no API key — vector search disabled")
            return

        try:
            import voyageai

            self._client = voyageai.Client(api_key=api_key)
            self._available = True
            logger.info(
                "Embeddings: Voyage AI initialized (model=%s, dims=%d)",
                settings.voyage_model, settings.voyage_embedding_dims,
            )
        except ImportError:
            logger.info("Embeddings: voyageai package not installed — vector search disabled")
        except Exception:
            logger.exception("Embeddings: failed to initialize Voyage client")

    @property
    def available(self) -> bool:
        """Whether the embedding provider is configured and ready."""
        self._lazy_init()
        return self._available

    async def embed(self, text: str) -> list[float] | None:
        """Embed a single text string. Returns None on failure.

        Runs the API call in a thread pool to avoid blocking the event loop.
        """
        self._lazy_init()
        if not self._available or not self._client:
            return None

        try:
            result = await asyncio.to_thread(
                self._client.embed,
                [text],
                model=settings.voyage_model,
            )
            if result and result.embeddings:
                return result.embeddings[0]
            return None
        except Exception:
            logger.warning("Embedding failed for text (len=%d)", len(text), exc_info=True)
            return None

    async def embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        """Embed multiple texts in one API call. Returns list of vectors."""
        self._lazy_init()
        if not self._available or not self._client or not texts:
            return [None] * len(texts)

        try:
            result = await asyncio.to_thread(
                self._client.embed,
                texts,
                model=settings.voyage_model,
            )
            if result and result.embeddings:
                return result.embeddings
            return [None] * len(texts)
        except Exception:
            logger.warning("Batch embedding failed for %d texts", len(texts), exc_info=True)
            return [None] * len(texts)

    async def health_check(self) -> dict[str, Any]:
        """Test the embedding API with a small probe.

        Returns a status dict for the /ready endpoint.
        Caches the result for 60 seconds to avoid hammering the API.
        """
        now = time.time()
        if now - self._last_health_check < 60:
            return {
                "status": "ok" if self._health_ok else "degraded",
                "cached": True,
                "available": self._available,
            }

        self._lazy_init()

        if not self._available:
            self._health_ok = False
            return {
                "status": "not_configured",
                "available": False,
                "reason": "Voyage AI client not initialized",
            }

        try:
            start = time.monotonic()
            result = await asyncio.to_thread(
                self._client.embed,
                ["health check"],
                model=settings.voyage_model,
            )
            latency_ms = (time.monotonic() - start) * 1000

            if result and result.embeddings and len(result.embeddings[0]) > 0:
                self._health_ok = True
                self._last_health_check = now
                dims = len(result.embeddings[0])
                return {
                    "status": "ok",
                    "model": settings.voyage_model,
                    "dimensions": dims,
                    "latency_ms": round(latency_ms, 1),
                }
            else:
                self._health_ok = False
                self._last_health_check = now
                return {
                    "status": "error",
                    "reason": "Empty embedding returned",
                }

        except Exception as e:
            self._health_ok = False
            self._last_health_check = now
            logger.warning("Embedding health check failed: %s", e)
            return {
                "status": "error",
                "reason": str(e)[:200],
            }


def build_incident_text(
    alert_name: str,
    namespace: str,
    root_cause: str,
    fix_summary: str,
) -> str:
    """Build the text string that gets embedded for an incident.

    Consistent format ensures similar incidents produce similar vectors.
    """
    return f"alert: {alert_name} | namespace: {namespace} | root_cause: {root_cause} | fix: {fix_summary}"


# Module-level singleton
embedding_provider = EmbeddingProvider()
