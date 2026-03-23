"""Tests for the orchestrator's response parsers.

The full orchestrator requires an Anthropic API key, so we test
the parser functions and helper logic independently.
"""

import pytest

from k8s_runbook_agent.agent.orchestrator import (
    _parse_diagnosis_block,
    _parse_escalate_block,
    _parse_fix_block,
)


class TestParseDiagnosis:
    def test_parses_valid_diagnosis(self):
        text = """
Here is my analysis:

```diagnosis
ROOT_CAUSE: Container OOMKilled due to memory limit of 256Mi being too low
CONFIDENCE: HIGH
EVIDENCE:
  - Last termination reason: OOMKilled
  - Exit code 137
  - Memory usage 254Mi / 256Mi limit
RULED_OUT:
  - Image pull failure
  - Liveness probe timeout
```
"""
        result = _parse_diagnosis_block(text)
        assert result is not None
        assert "OOMKilled" in result["root_cause"]
        assert result["confidence"] == "HIGH"
        assert len(result["evidence"]) == 3
        assert len(result["ruled_out"]) == 2

    def test_returns_none_for_no_block(self):
        assert _parse_diagnosis_block("No diagnosis here") is None

    def test_returns_none_for_missing_root_cause(self):
        text = "```diagnosis\nCONFIDENCE: HIGH\n```"
        assert _parse_diagnosis_block(text) is None

    def test_handles_medium_confidence(self):
        text = "```diagnosis\nROOT_CAUSE: maybe OOM\nCONFIDENCE: MEDIUM\n```"
        result = _parse_diagnosis_block(text)
        assert result["confidence"] == "MEDIUM"


class TestParseFixProposal:
    def test_parses_valid_fix(self):
        text = """
```fix_proposal
SUMMARY: Increase memory limit from 256Mi to 512Mi
RISK: LOW
DESCRIPTION: |
  Patch the deployment to set resources.limits.memory=512Mi
  for the api container.
DRY_RUN: |
  spec.containers[0].resources.limits.memory: 256Mi → 512Mi
ROLLBACK: |
  kubectl rollout undo deployment/api-server -n production
```
"""
        result = _parse_fix_block(text)
        assert result is not None
        assert "memory" in result["summary"].lower()
        assert result["risk"] == "LOW"
        assert "512Mi" in result["description"]
        assert "rollback" in result.get("rollback", "").lower() or "undo" in result.get("rollback", "").lower()

    def test_returns_none_for_no_block(self):
        assert _parse_fix_block("No fix here") is None

    def test_returns_none_without_summary(self):
        text = "```fix_proposal\nRISK: HIGH\n```"
        assert _parse_fix_block(text) is None

    def test_parses_human_values(self):
        text = """
```fix_proposal
SUMMARY: Set custom config
RISK: MEDIUM
DESCRIPTION: |
  Need to know the value
HUMAN_VALUES_NEEDED:
  - MEMORY_LIMIT
  - CPU_REQUEST
```
"""
        result = _parse_fix_block(text)
        assert result is not None
        assert len(result["human_values"]) == 2


class TestParseEscalation:
    def test_parses_escalation(self):
        text = """
```escalate
REASON: Cannot determine root cause — multiple potential issues detected
```
"""
        result = _parse_escalate_block(text)
        assert result is not None
        assert "Cannot determine" in result["reason"]

    def test_returns_none_for_no_block(self):
        assert _parse_escalate_block("No escalation") is None

    def test_default_reason(self):
        text = "```escalate\n```"
        result = _parse_escalate_block(text)
        assert result is not None
        assert "escalation" in result["reason"].lower()
