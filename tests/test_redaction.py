"""Tests for secret/PII redaction."""

import pytest

from k8s_runbook_agent.agent.redaction import (
    RedactionResult,
    _luhn_check,
    _shannon_entropy,
    redact,
    redact_content_block,
)


class TestKnownSecretPatterns:
    def test_aws_access_key(self):
        text = "Use key AKIAIOSFODNN7EXAMPLE for access."
        result = redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result.text
        assert "[REDACTED:aws-access-key]" in result.text
        assert "aws-access-key" in result.redactions

    def test_github_token_classic(self):
        fake_token = "gh" + "p_" + "X" * 40
        text = f"token: {fake_token}"
        result = redact(text)
        assert fake_token not in result.text
        assert "github-token" in result.redactions

    def test_slack_bot_token(self):
        # Fake token constructed at runtime to avoid GitHub secret scanner
        fake_token = "xox" + "b-" + "0" * 10 + "-" + "X" * 24
        text = f"SLACK_BOT_TOKEN={fake_token}"
        result = redact(text)
        assert fake_token not in result.text
        assert "slack-bot-token" in result.redactions

    def test_anthropic_api_key(self):
        text = "key=sk-ant-" + "a" * 100
        result = redact(text)
        assert "sk-ant-aaaa" not in result.text
        assert "anthropic-api-key" in result.redactions

    def test_jwt_token(self):
        text = (
            "Authorization: "
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        result = redact(text)
        assert "eyJzdWIiOiIxMjM0NTY3ODkw" not in result.text
        assert "jwt" in result.redactions

    def test_db_connection_string(self):
        text = "DATABASE_URL=postgresql://admin:secretpass123@db.internal:5432/prod"
        result = redact(text)
        assert "secretpass123" not in result.text
        assert "db-connection-string" in result.redactions

    def test_ssh_private_key(self):
        text = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAzrfake+keymaterial+here+fake+keymaterial
-----END RSA PRIVATE KEY-----"""
        result = redact(text)
        assert "fake+keymaterial" not in result.text
        assert "ssh-private-key" in result.redactions

    def test_multiple_secrets_counted(self):
        # Two distinct valid AWS access keys (each 20 chars total, separated by whitespace)
        text = "keys: AKIAIOSFODNN7EXAMPLE and ASIAJDNZXSJKFLWQRZIO"
        result = redact(text)
        assert result.redactions["aws-access-key"] == 2


class TestEntropyDetection:
    def test_shannon_entropy_high(self):
        # Random base64-like string should have high entropy
        s = "aK9pLmN3qR4sT5uV6wX7yZ8aBcDeFgHiJkLmN"
        assert _shannon_entropy(s) > 4.0

    def test_shannon_entropy_low(self):
        # Repeated chars = low entropy
        assert _shannon_entropy("aaaaaaaaaa") < 1.0
        assert _shannon_entropy("") == 0.0

    def test_high_entropy_blob_redacted(self):
        # Long random-looking string that doesn't match any known pattern
        text = "config: aK9pLmN3qR4sT5uV6wX7yZ8aBcDeFgHiJkLmN"
        result = redact(text)
        assert "aK9pLmN3qR4sT5uV6wX7yZ8aBcDeFgHiJkLmN" not in result.text
        # Should be caught by entropy detection
        assert "high-entropy" in result.redactions

    def test_pure_digits_not_flagged(self):
        # Long numeric IDs/timestamps are not flagged
        text = "request_id: 1234567890123456789012345678901234"
        result = redact(text)
        # Should not be redacted (pure digits, excluded)
        assert "1234567890" in result.text or "high-entropy" not in result.redactions

    def test_short_strings_ignored(self):
        text = "key=abc123"
        result = redact(text)
        # Too short for entropy check
        assert "abc123" in result.text or "high-entropy" not in result.redactions


class TestPIIPatterns:
    def test_email_redacted(self):
        text = "contact alice@company.com for details"
        result = redact(text)
        assert "alice@company.com" not in result.text
        assert "email" in result.redactions

    def test_test_email_not_redacted(self):
        # Test/example domains are whitelisted
        text = "use test@example.com in unit tests"
        result = redact(text)
        assert "test@example.com" in result.text

    def test_k8s_internal_domain_not_redacted(self):
        text = "service reachable at api.apps.svc.cluster.local"
        result = redact(text)
        # Not an email, but should not redact K8s internal URLs
        assert "cluster.local" in result.text

    def test_pii_disabled(self):
        text = "alice@company.com"
        result = redact(text, enable_pii=False)
        assert "alice@company.com" in result.text

    def test_ssn_redacted(self):
        text = "SSN: 123-45-6789"
        result = redact(text)
        assert "123-45-6789" not in result.text
        assert "ssn" in result.redactions


class TestLuhnCheck:
    def test_valid_card(self):
        # Valid Visa test card number
        assert _luhn_check("4532015112830366") is True

    def test_invalid_card(self):
        assert _luhn_check("1234567890123456") is False

    def test_too_short(self):
        assert _luhn_check("123") is False

    def test_credit_card_detected(self):
        text = "Card: 4532015112830366"
        result = redact(text)
        assert "4532015112830366" not in result.text
        assert "credit-card" in result.redactions

    def test_random_long_digits_not_flagged(self):
        # 16 random digits that DON'T pass Luhn
        text = "request_id: 1234567890123456"
        result = redact(text)
        # Should NOT be flagged as credit card
        assert "credit-card" not in result.redactions


class TestRedactionResult:
    def test_empty_text(self):
        result = redact("")
        assert result.text == ""
        assert result.had_secrets is False
        assert result.redaction_count == 0

    def test_none_text(self):
        result = redact(None)  # type: ignore
        assert result.text == ""

    def test_non_string_input(self):
        result = redact(12345)  # type: ignore
        assert result.had_secrets is False

    def test_clean_text_untouched(self):
        text = "Pod is in CrashLoopBackOff, exit code 137, OOMKilled."
        result = redact(text)
        assert result.text == text
        assert not result.had_secrets

    def test_had_secrets_true(self):
        text = "AKIAIOSFODNN7EXAMPLE"
        result = redact(text)
        assert result.had_secrets is True

    def test_redaction_count(self):
        text = "AKIAIOSFODNN7EXAMPLE and contact alice@company.com"
        result = redact(text)
        assert result.redaction_count == 2


class TestRedactContentBlock:
    def test_text_block_redacted(self):
        block = {"type": "text", "text": "AKIAIOSFODNN7EXAMPLE found"}
        new_block, result = redact_content_block(block)
        assert new_block["type"] == "text"
        assert "AKIAIOSFODNN7EXAMPLE" not in new_block["text"]
        assert result.had_secrets

    def test_non_text_block_unchanged(self):
        block = {"type": "image", "source": "..."}
        new_block, result = redact_content_block(block)
        assert new_block == block
