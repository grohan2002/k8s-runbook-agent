"""RBAC authorization for Slack approval actions.

Controls WHO can approve/reject fixes based on Slack user IDs, groups,
and risk-level thresholds.

Configuration via environment variables:
  APPROVAL_ALLOWED_USERS    — comma-separated Slack user IDs (e.g. "U123,U456")
  APPROVAL_ALLOWED_GROUPS   — comma-separated Slack user group IDs (e.g. "S123")
  APPROVAL_MIN_RISK_FOR_REVIEW — minimum risk level that requires approval from
                                  a senior approver (default: HIGH)
  APPROVAL_SENIOR_USERS     — comma-separated Slack user IDs for senior approvers
                               (required for HIGH/CRITICAL risk)

When no users/groups are configured, ALL users are allowed (open mode / dev).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..config import settings
from ..models import RiskLevel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RBAC policy
# ---------------------------------------------------------------------------
class AuthzDecision(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_SENIOR = "needs_senior"


@dataclass
class AuthzResult:
    decision: AuthzDecision
    reason: str
    user_id: str
    user_name: str

    @property
    def allowed(self) -> bool:
        return self.decision == AuthzDecision.ALLOWED

    def to_slack_text(self) -> str:
        if self.decision == AuthzDecision.ALLOWED:
            return f"✅ Authorized: {self.reason}"
        elif self.decision == AuthzDecision.NEEDS_SENIOR:
            return f"⚠️ {self.reason}"
        return f"🚫 {self.reason}"


class ApprovalPolicy:
    """Evaluates whether a Slack user is authorized to approve a fix.

    Supports:
      - Allowlist of user IDs
      - Allowlist of Slack user group IDs (checked via API)
      - Risk-level escalation (HIGH/CRITICAL requires senior approver)
      - Open mode when no policy is configured
    """

    def __init__(self) -> None:
        self._allowed_users: set[str] = set()
        self._allowed_groups: set[str] = set()
        self._senior_users: set[str] = set()
        self._min_risk_for_senior: RiskLevel = RiskLevel.HIGH
        self._configured: bool = False
        self._group_members_cache: dict[str, set[str]] = {}

    def configure(
        self,
        allowed_users: str = "",
        allowed_groups: str = "",
        senior_users: str = "",
        min_risk_for_senior: str = "high",
    ) -> None:
        """Load policy from comma-separated config strings."""
        self._allowed_users = _parse_csv(allowed_users)
        self._allowed_groups = _parse_csv(allowed_groups)
        self._senior_users = _parse_csv(senior_users)

        try:
            self._min_risk_for_senior = RiskLevel(min_risk_for_senior.lower())
        except ValueError:
            self._min_risk_for_senior = RiskLevel.HIGH

        self._configured = bool(
            self._allowed_users or self._allowed_groups or self._senior_users
        )

        if self._configured:
            logger.info(
                "RBAC configured: %d users, %d groups, %d senior users, "
                "senior required for %s+",
                len(self._allowed_users),
                len(self._allowed_groups),
                len(self._senior_users),
                self._min_risk_for_senior.value,
            )
        else:
            logger.warning("RBAC: open mode — all Slack users can approve (no policy configured)")

    async def authorize(
        self,
        user_id: str,
        user_name: str,
        risk_level: RiskLevel = RiskLevel.LOW,
    ) -> AuthzResult:
        """Check if a Slack user is authorized to approve a fix at the given risk level.

        Returns AuthzResult with decision, reason, and user info.
        """
        # Open mode — allow everyone
        if not self._configured:
            return AuthzResult(
                decision=AuthzDecision.ALLOWED,
                reason="Open mode (no RBAC policy configured)",
                user_id=user_id,
                user_name=user_name,
            )

        # Check direct user allowlist
        user_in_allowlist = user_id in self._allowed_users
        user_is_senior = user_id in self._senior_users

        # Check group membership
        user_in_group = False
        if self._allowed_groups and not user_in_allowlist:
            user_in_group = await self._check_group_membership(user_id)

        # Is the user authorized at all?
        is_authorized = user_in_allowlist or user_in_group or user_is_senior

        if not is_authorized:
            logger.warning(
                "RBAC denied: user %s (%s) not in approved users or groups",
                user_id, user_name,
            )
            return AuthzResult(
                decision=AuthzDecision.DENIED,
                reason=f"User <@{user_id}> is not authorized to approve fixes. "
                       "Contact your platform team to be added to the approval list.",
                user_id=user_id,
                user_name=user_name,
            )

        # Risk-level escalation check
        risk_order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        if (
            self._senior_users
            and risk_order.index(risk_level) >= risk_order.index(self._min_risk_for_senior)
            and not user_is_senior
        ):
            logger.info(
                "RBAC: user %s authorized but fix is %s risk — needs senior approval",
                user_id, risk_level.value,
            )
            senior_mentions = " ".join(f"<@{u}>" for u in self._senior_users)
            return AuthzResult(
                decision=AuthzDecision.NEEDS_SENIOR,
                reason=f"This is a *{risk_level.value.upper()}* risk fix. "
                       f"Requires approval from a senior approver: {senior_mentions}",
                user_id=user_id,
                user_name=user_name,
            )

        return AuthzResult(
            decision=AuthzDecision.ALLOWED,
            reason=f"User <@{user_id}> authorized"
                   + (" (senior)" if user_is_senior else ""),
            user_id=user_id,
            user_name=user_name,
        )

    async def _check_group_membership(self, user_id: str) -> bool:
        """Check if user belongs to any allowed Slack user groups."""
        for group_id in self._allowed_groups:
            members = self._group_members_cache.get(group_id)
            if members is None:
                members = await self._fetch_group_members(group_id)
                self._group_members_cache[group_id] = members

            if user_id in members:
                return True
        return False

    async def _fetch_group_members(self, group_id: str) -> set[str]:
        """Fetch group members from the Slack API."""
        try:
            from slack_sdk import WebClient

            client = WebClient(token=settings.slack_bot_token)
            result = await asyncio.to_thread(
                client.usergroups_users_list, usergroup=group_id
            )
            users = set(result.get("users", []))
            logger.info("Fetched %d members for Slack group %s", len(users), group_id)
            return users
        except Exception:
            logger.exception("Failed to fetch members for Slack group %s", group_id)
            return set()

    def clear_cache(self) -> None:
        """Clear the group membership cache (call periodically or on config reload)."""
        self._group_members_cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_csv(value: str) -> set[str]:
    """Parse a comma-separated string into a set, stripping whitespace."""
    if not value:
        return set()
    return {v.strip() for v in value.split(",") if v.strip()}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
approval_policy = ApprovalPolicy()
