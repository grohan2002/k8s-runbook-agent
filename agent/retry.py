"""Anthropic API call with retry logic.

Handles transient failures:
  - 429 RateLimitError → exponential backoff with jitter
  - 500/529 APIStatusError → retry up to MAX_RETRIES
  - APIConnectionError → retry with backoff
  - Overloaded → respect Retry-After header

Non-retryable:
  - 400 BadRequest → raise immediately (malformed input)
  - 401 AuthenticationError → raise immediately
  - 404 NotFoundError → raise immediately
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_DELAY = 1.0  # seconds
MAX_DELAY = 30.0  # seconds


class AnthropicCallError(Exception):
    """Raised when all retries are exhausted."""

    def __init__(self, message: str, last_error: Exception | None = None) -> None:
        super().__init__(message)
        self.last_error = last_error


async def call_anthropic_with_retry(
    client: anthropic.Anthropic,
    *,
    model: str,
    max_tokens: int,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> anthropic.types.Message:
    """Call the Anthropic messages API with retry logic for transient errors.

    Returns the Message response on success.
    Raises AnthropicCallError after all retries are exhausted.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Run the synchronous Anthropic client in a thread
            response = await asyncio.to_thread(
                client.messages.create,
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                messages=messages,
            )
            return response

        except anthropic.RateLimitError as e:
            last_error = e
            # Check for Retry-After header
            retry_after = _extract_retry_after(e)
            delay = retry_after or _backoff_delay(attempt)
            logger.warning(
                "Anthropic rate limited (attempt %d/%d). Retrying in %.1fs",
                attempt, MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)

        except anthropic.APIStatusError as e:
            last_error = e
            # Only retry on server errors (500, 529 overloaded)
            if e.status_code in (500, 529):
                delay = _backoff_delay(attempt)
                logger.warning(
                    "Anthropic server error %d (attempt %d/%d). Retrying in %.1fs",
                    e.status_code, attempt, MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
            else:
                # 400, 401, 404, etc. — not retryable
                logger.error("Anthropic API error %d: %s", e.status_code, e.message)
                raise AnthropicCallError(
                    f"Anthropic API error ({e.status_code}): {e.message}", last_error=e
                ) from e

        except anthropic.APIConnectionError as e:
            last_error = e
            delay = _backoff_delay(attempt)
            logger.warning(
                "Anthropic connection error (attempt %d/%d). Retrying in %.1fs",
                attempt, MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)

        except Exception as e:
            # Unexpected error — don't retry
            logger.error("Unexpected error calling Anthropic: %s", e)
            raise AnthropicCallError(f"Unexpected error: {e}", last_error=e) from e

    raise AnthropicCallError(
        f"All {MAX_RETRIES} retries exhausted for Anthropic API call",
        last_error=last_error,
    )


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter."""
    delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    jitter = random.uniform(0, delay * 0.3)
    return delay + jitter


def _extract_retry_after(error: anthropic.RateLimitError) -> float | None:
    """Try to extract Retry-After header from a rate limit error."""
    try:
        if hasattr(error, "response") and error.response is not None:
            retry_after = error.response.headers.get("retry-after")
            if retry_after:
                return float(retry_after)
    except (ValueError, AttributeError):
        pass
    return None
