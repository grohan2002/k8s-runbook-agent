"""Toil Detector — scans incident memory for recurring alerts that indicate toil.

An alert firing N times per week with the same fix suggests the underlying
issue should be permanently fixed (e.g., memory limit raised in the manifest,
HPA rules adjusted) rather than repeatedly remediated.

The detector runs as a background task on a configurable schedule
(default: weekly). When toil candidates are found, it posts a report
to Slack so humans can triage them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class ToilCandidate:
    """A recurring alert+fix pattern that is a candidate for permanent fix."""

    alert_name: str
    fix_summary: str
    occurrences: int
    first_seen: datetime | None
    last_seen: datetime | None
    namespaces: list[str]
    success_rate: float  # fraction of occurrences resolved successfully

    def to_slack_text(self) -> str:
        """Render as a Slack-friendly bullet."""
        ns_str = ", ".join(self.namespaces[:3])
        if len(self.namespaces) > 3:
            ns_str += f" (+{len(self.namespaces) - 3} more)"
        success_pct = round(self.success_rate * 100)
        return (
            f"• *{self.alert_name}* — fired *{self.occurrences}x*\n"
            f"  Fix applied: _{self.fix_summary}_\n"
            f"  Namespaces: {ns_str} | Success rate: {success_pct}%"
        )


class ToilDetector:
    """Background task that periodically scans incident_memory for toil candidates."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._last_run: datetime | None = None
        self._last_candidates: list[ToilCandidate] = []

    @property
    def enabled(self) -> bool:
        return getattr(settings, "toil_detection_enabled", True)

    async def run(self) -> None:
        """Main loop — sleeps then runs detect() until cancelled."""
        if not self.enabled:
            logger.info("Toil detector: disabled via config")
            return

        interval_hours = getattr(settings, "toil_detection_interval_hours", 168)
        interval_seconds = max(60, interval_hours * 3600)

        logger.info(
            "Toil detector: started (interval=%dh, window=%dd, threshold=%d)",
            interval_hours,
            getattr(settings, "toil_detection_window_days", 7),
            getattr(settings, "toil_detection_threshold", 5),
        )

        try:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    candidates = await self.detect()
                    if candidates:
                        await self._post_report(candidates)
                except Exception:
                    logger.exception("Toil detection run failed")
        except asyncio.CancelledError:
            logger.info("Toil detector stopped")

    async def detect(self) -> list[ToilCandidate]:
        """Run the aggregation query and return toil candidates."""
        from ..db import get_toil_candidates

        window_days = getattr(settings, "toil_detection_window_days", 7)
        threshold = getattr(settings, "toil_detection_threshold", 5)

        try:
            rows = await get_toil_candidates(window_days=window_days, threshold=threshold)
        except Exception:
            logger.exception("get_toil_candidates query failed")
            return []

        candidates: list[ToilCandidate] = []
        for row in rows:
            occ = int(row.get("occurrences", 0))
            successes = int(row.get("successes", 0))
            candidates.append(
                ToilCandidate(
                    alert_name=row.get("alert_name", "unknown"),
                    fix_summary=row.get("fix_summary") or "(no fix recorded)",
                    occurrences=occ,
                    first_seen=row.get("first_seen"),
                    last_seen=row.get("last_seen"),
                    namespaces=list(row.get("namespaces", []) or []),
                    success_rate=successes / occ if occ > 0 else 0.0,
                )
            )

        self._last_run = datetime.now()
        self._last_candidates = candidates
        logger.info("Toil detection found %d candidates", len(candidates))

        try:
            from ..observability.metrics import toil_candidates_detected
            toil_candidates_detected.inc(value=len(candidates))
        except Exception:
            pass

        return candidates

    async def _post_report(self, candidates: list[ToilCandidate]) -> None:
        """Post the toil report to the configured Slack channel."""
        from ..slack.bot import _get_slack_client

        channel = settings.slack_channel_id
        if not channel or not settings.slack_bot_token:
            logger.info("Toil report has %d candidates but Slack is not configured", len(candidates))
            return

        window_days = getattr(settings, "toil_detection_window_days", 7)
        threshold = getattr(settings, "toil_detection_threshold", 5)

        header = (
            f":recycle: *Weekly Toil Report* — "
            f"{len(candidates)} candidate(s) found "
            f"(≥{threshold} occurrences in the last {window_days} days)"
        )
        body_lines = [c.to_slack_text() for c in candidates[:15]]
        footer = (
            "\n\n_These alerts are remediated repeatedly with the same fix. "
            "Consider a permanent fix (adjust manifests, tune HPA, fix code) "
            "rather than continuing to apply the same remediation._"
        )

        text = header + "\n\n" + "\n\n".join(body_lines) + footer

        try:
            client = _get_slack_client()
            await asyncio.to_thread(
                client.chat_postMessage,
                channel=channel,
                text=text,
            )
            logger.info("Toil report posted to Slack (%d candidates)", len(candidates))
        except Exception:
            logger.exception("Failed to post toil report to Slack")


# Module-level singleton
toil_detector = ToilDetector()
