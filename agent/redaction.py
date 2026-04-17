"""Secret and PII redaction for tool outputs.

Before tool results (pod logs, configmap data, resource YAML) are appended
to Claude's conversation, this module scans them for:

  1. Known secret patterns (Bearer tokens, AWS keys, Slack tokens, JWTs, etc.)
  2. PII patterns (emails, potential SSNs, credit cards)
  3. High-entropy strings that look like secrets but don't match known patterns

Matches are replaced with ``[REDACTED:<type>]`` placeholders so Claude can
still reason about the structure without seeing actual secrets.

This is a defense-in-depth layer — the K8s tools already redact
``Secret.data`` fields, but logs and configmaps can still contain secrets.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Known secret patterns (name → (regex, placeholder))
# ---------------------------------------------------------------------------
# Order matters: more specific patterns first.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # AWS
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws-secret-key", re.compile(r"(?i)aws.{0,20}[\"'=\s:]([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])")),

    # GCP
    ("gcp-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("gcp-service-account", re.compile(r'"type"\s*:\s*"service_account"')),

    # Slack
    ("slack-bot-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z]{10,48}\b")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/T[0-9A-Z]+/B[0-9A-Z]+/[0-9A-Za-z]+")),

    # GitHub
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,255}\b")),
    ("github-fine-grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),

    # Anthropic / OpenAI
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{90,}\b")),
    ("openai-api-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),

    # Voyage
    ("voyage-api-key", re.compile(r"\bpa-[A-Za-z0-9_\-]{30,}\b")),

    # Generic bearer tokens in Authorization headers
    ("bearer-token", re.compile(r"(?i)authorization:\s*bearer\s+([A-Za-z0-9_\-\.=/+]{20,})")),

    # JWT (3 base64 segments separated by dots)
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{10,}\b")),

    # Database URLs with credentials (postgresql://user:pass@host)
    ("db-connection-string", re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:]+:[^\s@]+@[^\s/]+")),

    # SSH / PEM private keys
    ("ssh-private-key", re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |)PRIVATE KEY-----.*?-----END (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |)PRIVATE KEY-----", re.DOTALL)),

    # Kubernetes service account tokens (start with eyJ like JWTs — covered above)

    # Basic auth in URLs (covered by db-connection-string for known schemes)
    ("basic-auth-url", re.compile(r"https?://[^\s:]+:[^\s@]+@[^\s/]+")),
]


# ---------------------------------------------------------------------------
# PII patterns
# ---------------------------------------------------------------------------
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Email addresses
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),

    # Credit card numbers (Luhn-checkable; loose pattern here — verify with Luhn below)
    ("credit-card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),

    # US SSN (loose — three digits, two digits, four digits)
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]


# Common "whitelist" emails that are NOT real PII (test/example domains)
_EMAIL_WHITELIST = re.compile(
    r"@(?:example\.(?:com|org|net)|test\.(?:com|org)|localhost|\.local|"
    r"cluster\.local|svc\.cluster\.local)(?:\b|$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# High-entropy string detection
# ---------------------------------------------------------------------------
# Catch unknown secrets by looking for long base64/hex-looking strings with
# high Shannon entropy. Only triggers on strings >= MIN_LEN chars.
_ENTROPY_MIN_LEN = 32
_ENTROPY_MIN_BITS = 4.5  # typical threshold for base64 secrets
_CANDIDATE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-+/=]{32,}\b")


@dataclass
class RedactionResult:
    """Result of redacting a single text block."""

    text: str                        # sanitized text with placeholders
    redactions: dict[str, int] = field(default_factory=dict)
    bytes_removed: int = 0

    @property
    def redaction_count(self) -> int:
        return sum(self.redactions.values())

    @property
    def had_secrets(self) -> bool:
        return self.redaction_count > 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def redact(text: str, enable_entropy_check: bool = True, enable_pii: bool = True) -> RedactionResult:
    """Redact secrets and PII from a text blob.

    Returns a RedactionResult with the sanitized text and counts per type.
    The original text is never raised or logged — only counts.
    """
    if not text or not isinstance(text, str):
        return RedactionResult(text=text or "")

    original_len = len(text)
    result = text
    redactions: dict[str, int] = {}

    # 1. Known secret patterns
    for name, pattern in _SECRET_PATTERNS:
        result, count = _apply_pattern(result, pattern, f"[REDACTED:{name}]")
        if count:
            redactions[name] = redactions.get(name, 0) + count

    # 2. PII patterns
    if enable_pii:
        for name, pattern in _PII_PATTERNS:
            if name == "email":
                # Filter out whitelisted test/local domains
                def _sub_email(m: re.Match[str]) -> str:
                    match_text = m.group(0)
                    if _EMAIL_WHITELIST.search(match_text):
                        return match_text  # don't redact test emails
                    return f"[REDACTED:{name}]"

                prev = result
                result = pattern.sub(_sub_email, result)
                count = prev.count(f"[REDACTED:{name}]") < result.count(f"[REDACTED:{name}]")
                # More accurate count:
                count = result.count(f"[REDACTED:{name}]") - prev.count(f"[REDACTED:{name}]")
                if count:
                    redactions[name] = redactions.get(name, 0) + count
            elif name == "credit-card":
                # Apply Luhn check to avoid false positives on random long digit strings
                result, count = _redact_credit_cards(result, pattern)
                if count:
                    redactions[name] = redactions.get(name, 0) + count
            else:
                result, count = _apply_pattern(result, pattern, f"[REDACTED:{name}]")
                if count:
                    redactions[name] = redactions.get(name, 0) + count

    # 3. High-entropy strings (catches unknown secrets)
    if enable_entropy_check:
        result, count = _redact_high_entropy(result)
        if count:
            redactions["high-entropy"] = redactions.get("high-entropy", 0) + count

    bytes_removed = max(0, original_len - len(result))
    return RedactionResult(text=result, redactions=redactions, bytes_removed=bytes_removed)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _apply_pattern(text: str, pattern: re.Pattern[str], placeholder: str) -> tuple[str, int]:
    """Replace all matches with placeholder. Returns (new_text, count)."""
    matches = pattern.findall(text)
    if not matches:
        return text, 0
    new_text = pattern.sub(placeholder, text)
    return new_text, len(matches)


def _shannon_entropy(s: str) -> float:
    """Compute Shannon entropy in bits per character."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    entropy = 0.0
    for count in freq.values():
        p = count / n
        entropy -= p * math.log2(p)
    return entropy


def _redact_high_entropy(text: str) -> tuple[str, int]:
    """Find high-entropy tokens (likely unknown secrets) and redact them."""
    count = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal count
        token = m.group(0)
        if len(token) < _ENTROPY_MIN_LEN:
            return token
        # Skip if it's already been redacted (contains REDACTED)
        if "REDACTED" in token:
            return token
        # Skip common non-secret long strings: pure alphabetic (words), pure numeric (IDs/timestamps)
        if token.isalpha() or token.isdigit():
            return token
        # Skip strings with low character diversity
        if len(set(token)) < 10:
            return token
        entropy = _shannon_entropy(token)
        if entropy >= _ENTROPY_MIN_BITS:
            count += 1
            return "[REDACTED:high-entropy]"
        return token

    new_text = _CANDIDATE_TOKEN_RE.sub(_sub, text)
    return new_text, count


def _luhn_check(number: str) -> bool:
    """Luhn algorithm for credit card validation."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _redact_credit_cards(text: str, pattern: re.Pattern[str]) -> tuple[str, int]:
    """Redact credit-card-like numbers that pass Luhn check."""
    count = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal count
        match_text = m.group(0)
        digits = "".join(c for c in match_text if c.isdigit())
        if _luhn_check(digits):
            count += 1
            return "[REDACTED:credit-card]"
        return match_text

    new_text = pattern.sub(_sub, text)
    return new_text, count


# ---------------------------------------------------------------------------
# Convenience: redact a content-block dict (tool result format)
# ---------------------------------------------------------------------------
def redact_content_block(block: dict[str, Any]) -> tuple[dict[str, Any], RedactionResult]:
    """Redact the text of a content block. Returns (new_block, result)."""
    if not isinstance(block, dict) or block.get("type") != "text":
        return block, RedactionResult(text="")
    result = redact(block.get("text", ""))
    new_block = {**block, "text": result.text}
    return new_block, result
