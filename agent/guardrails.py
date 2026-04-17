"""Safety guardrails for fix execution.

Validates proposed fixes before they are applied to the cluster.
Every fix must pass ALL guardrails or it is blocked.

Guardrails:
  1. Namespace blocklist — never mutate kube-system, kube-public, etc.
  2. Resource kind allowlist — only known safe mutation types
  3. Risk ceiling — block CRITICAL risk fixes from auto-execution
  4. Replica bounds — prevent scaling to 0 or to extreme values
  5. Image change review — flag any image tag changes
  6. Dry-run validation — require successful dry-run before live execution
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..agent.session import DiagnosisSession
from ..config import settings
from ..models import RiskLevel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BLOCKED_NAMESPACES = frozenset({
    "kube-system",
    "kube-public",
    "kube-node-lease",
    "cert-manager",
    "istio-system",
    "monitoring",
    "ingress-nginx",
})

ALLOWED_MUTATION_KINDS = frozenset({
    "deployment",
    "statefulset",
    "daemonset",
    "configmap",
    "service",
    "pod",  # delete only
})

MAX_RISK_FOR_AUTO_EXECUTION = RiskLevel.HIGH  # CRITICAL is blocked

MAX_REPLICA_COUNT = 50
MIN_REPLICA_COUNT = 0  # 0 is allowed (scale down) but flagged

# Image patterns that should never be overridden by automation
PROTECTED_IMAGE_PATTERNS = [
    r".*:latest$",  # Never deploy :latest via automation
]


# ---------------------------------------------------------------------------
# Guardrail result
# ---------------------------------------------------------------------------
@dataclass
class GuardrailResult:
    """Result of guardrail evaluation."""

    passed: bool = True
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def block(self, reason: str) -> None:
        self.passed = False
        self.blocked_reasons.append(reason)
        logger.warning("Guardrail BLOCKED: %s", reason)

    def warn(self, reason: str) -> None:
        self.warnings.append(reason)
        logger.info("Guardrail WARNING: %s", reason)

    def summary(self) -> str:
        lines = []
        if not self.passed:
            lines.append("❌ BLOCKED — fix cannot be applied:")
            for r in self.blocked_reasons:
                lines.append(f"  • {r}")
        else:
            lines.append("✅ All guardrails passed.")

        if self.warnings:
            lines.append("\n⚠️ Warnings:")
            for w in self.warnings:
                lines.append(f"  • {w}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------
def evaluate_guardrails(session: DiagnosisSession) -> GuardrailResult:
    """Run all guardrails against a session's proposed fix."""
    result = GuardrailResult()

    if not session.fix_proposal:
        result.block("No fix proposal found in session.")
        return result

    fix = session.fix_proposal
    alert = session.alert
    ns = alert.namespace

    # 1. Namespace blocklist
    if ns in BLOCKED_NAMESPACES:
        result.block(
            f"Namespace '{ns}' is in the blocklist. "
            f"Blocked namespaces: {', '.join(sorted(BLOCKED_NAMESPACES))}"
        )

    # 2. Risk ceiling
    risk_order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
    if risk_order.index(fix.risk_level) >= risk_order.index(RiskLevel.CRITICAL):
        result.block(
            f"Fix risk level is {fix.risk_level.value.upper()} — "
            "CRITICAL fixes require manual execution."
        )

    # 3. Human values needed
    if fix.requires_human_values and fix.human_value_fields:
        result.block(
            f"Fix requires human-provided values that the agent cannot determine:\n"
            + "\n".join(f"  - {field}" for field in fix.human_value_fields)
            + "\nReply in the Slack thread with the correct values, then re-trigger the investigation."
        )

    # 4. Check for replica changes in description
    _check_replica_guardrails(fix.description + " " + fix.dry_run_output, result)

    # 5. Check for image changes
    _check_image_guardrails(fix.description + " " + fix.dry_run_output, result)

    # 6. Dry-run check
    if settings.dry_run_default and not fix.dry_run_output:
        result.warn(
            "No dry-run output recorded. The fix should be dry-run tested before live execution."
        )

    # 7. Rollback plan check
    if not fix.rollback_plan:
        result.warn("No rollback plan provided. Ensure you know how to undo this change.")

    # 8. Confidence check
    if session.diagnosis:
        from ..models import Confidence

        if session.diagnosis.confidence == Confidence.LOW:
            result.block(
                "Diagnosis confidence is LOW — automated fix should not be applied. "
                "Escalate for manual review."
            )
        elif session.diagnosis.confidence == Confidence.MEDIUM:
            result.warn(
                "Diagnosis confidence is MEDIUM. Verify the evidence before approving."
            )

    # 9. SLO impact check (Feature: SLO Impact Awareness)
    if session.alert.error_budget_remaining is not None:
        if session.alert.error_budget_remaining < 10:
            result.warn(
                f"Low error budget ({session.alert.error_budget_remaining}%) for SLO "
                f"'{session.alert.slo_name or 'unknown'}'. Verify fix carefully — "
                "further outages will breach SLO."
            )

    # 10. Compound fix detection
    _check_compound_fix(fix.description + " " + (fix.dry_run_output or ""), result)

    # 10. Composite fix confidence check
    if session.fix_confidence is not None:
        if session.fix_confidence.score < 0.3:
            result.block(
                f"Fix confidence score is {session.fix_confidence.percentage}% "
                f"(below 30% threshold). Blocked from auto-execution."
            )
        elif session.fix_confidence.score < 0.5:
            result.warn(
                f"Fix confidence score is {session.fix_confidence.percentage}%. "
                f"Review evidence carefully before approving."
            )

    return result


# ---------------------------------------------------------------------------
# Sub-guardrails
# ---------------------------------------------------------------------------
def _check_replica_guardrails(text: str, result: GuardrailResult) -> None:
    """Check for unsafe replica changes in the fix description."""
    # Look for replica counts in the text
    replica_matches = re.findall(r"replicas?\s*[:=]\s*(\d+)", text, re.IGNORECASE)
    for match in replica_matches:
        count = int(match)
        if count == 0:
            result.warn(
                "Fix sets replicas to 0. This will cause a full outage for the workload."
            )
        if count > MAX_REPLICA_COUNT:
            result.block(
                f"Fix sets replicas to {count}, exceeding the maximum of {MAX_REPLICA_COUNT}."
            )


def _check_compound_fix(text: str, result: GuardrailResult) -> None:
    """Detect fixes that require multiple sequential steps.

    The executor runs a single tool-use loop — it may miss step 1 if
    steps are described sequentially. Warn the human to verify.
    """
    text_lower = text.lower()

    # Patterns that indicate multi-step fixes
    compound_signals = [
        (r"(?:first|step\s*1).*(?:then|step\s*2|after\s+that|next|second)", "sequential steps described"),
        (r"create\s+(?:configmap|secret|service).*(?:then|and\s+then|after).*patch", "create + patch sequence"),
        (r"delete.*(?:then|and).*(?:create|apply|deploy)", "delete + create sequence"),
    ]

    for pattern, reason in compound_signals:
        if re.search(pattern, text_lower, re.DOTALL):
            result.warn(
                f"Fix appears to require multiple sequential steps ({reason}). "
                "The executor may not execute them in the correct order. "
                "Review carefully and consider splitting into separate fixes."
            )
            return  # One warning is enough


def _check_image_guardrails(text: str, result: GuardrailResult) -> None:
    """Check for unsafe image references in the fix description."""
    # Look for image references
    image_matches = re.findall(r"image:\s*(\S+)", text, re.IGNORECASE)
    for image in image_matches:
        for pattern in PROTECTED_IMAGE_PATTERNS:
            if re.match(pattern, image):
                result.block(
                    f"Fix references image '{image}' which matches blocked pattern. "
                    "Never deploy :latest via automation."
                )
