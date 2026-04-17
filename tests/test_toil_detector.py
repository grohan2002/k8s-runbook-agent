"""Tests for the toil detector."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from k8s_runbook_agent.agent.toil_detector import ToilCandidate, ToilDetector


class TestToilCandidate:
    def test_to_slack_text_basic(self):
        c = ToilCandidate(
            alert_name="KubePodCrashLooping",
            fix_summary="Increase memory to 512Mi",
            occurrences=7,
            first_seen=datetime(2026, 4, 1, tzinfo=timezone.utc),
            last_seen=datetime(2026, 4, 17, tzinfo=timezone.utc),
            namespaces=["production", "staging"],
            success_rate=6 / 7,
        )
        text = c.to_slack_text()
        assert "KubePodCrashLooping" in text
        assert "7x" in text
        assert "Increase memory to 512Mi" in text
        assert "production" in text
        assert "86%" in text  # round(6/7 * 100)

    def test_to_slack_text_many_namespaces(self):
        c = ToilCandidate(
            alert_name="A", fix_summary="f", occurrences=10,
            first_seen=None, last_seen=None,
            namespaces=["ns1", "ns2", "ns3", "ns4", "ns5"],
            success_rate=1.0,
        )
        text = c.to_slack_text()
        assert "+2 more" in text


class TestToilDetectorDetect:
    @pytest.mark.asyncio
    async def test_detect_empty_when_no_data(self):
        detector = ToilDetector()
        with patch(
            "k8s_runbook_agent.db.get_toil_candidates",
            return_value=[],
        ):
            result = await detector.detect()
        assert result == []

    @pytest.mark.asyncio
    async def test_detect_converts_rows_to_candidates(self):
        detector = ToilDetector()
        rows = [
            {
                "alert_name": "KubePodOOMKilled",
                "fix_summary": "Increase memory to 512Mi",
                "occurrences": 8,
                "successes": 7,
                "first_seen": datetime(2026, 4, 10, tzinfo=timezone.utc),
                "last_seen": datetime(2026, 4, 17, tzinfo=timezone.utc),
                "namespaces": ["production"],
            },
            {
                "alert_name": "KubePodCrashLooping",
                "fix_summary": "Restart pod",
                "occurrences": 5,
                "successes": 5,
                "first_seen": None,
                "last_seen": None,
                "namespaces": ["staging", "production"],
            },
        ]
        with patch(
            "k8s_runbook_agent.db.get_toil_candidates",
            return_value=rows,
        ):
            result = await detector.detect()
        assert len(result) == 2
        assert result[0].alert_name == "KubePodOOMKilled"
        assert result[0].occurrences == 8
        assert result[0].success_rate == 7 / 8
        assert result[1].alert_name == "KubePodCrashLooping"
        assert result[1].success_rate == 1.0

    @pytest.mark.asyncio
    async def test_detect_handles_zero_occurrences(self):
        detector = ToilDetector()
        rows = [{
            "alert_name": "X", "fix_summary": "y", "occurrences": 0,
            "successes": 0, "first_seen": None, "last_seen": None,
            "namespaces": [],
        }]
        with patch(
            "k8s_runbook_agent.db.get_toil_candidates",
            return_value=rows,
        ):
            result = await detector.detect()
        assert len(result) == 1
        assert result[0].success_rate == 0.0

    @pytest.mark.asyncio
    async def test_detect_handles_missing_fix_summary(self):
        detector = ToilDetector()
        rows = [{
            "alert_name": "X", "fix_summary": None, "occurrences": 5,
            "successes": 3, "first_seen": None, "last_seen": None,
            "namespaces": ["ns"],
        }]
        with patch(
            "k8s_runbook_agent.db.get_toil_candidates",
            return_value=rows,
        ):
            result = await detector.detect()
        assert result[0].fix_summary == "(no fix recorded)"

    @pytest.mark.asyncio
    async def test_detect_query_error_returns_empty(self):
        detector = ToilDetector()
        with patch(
            "k8s_runbook_agent.db.get_toil_candidates",
            side_effect=Exception("DB error"),
        ):
            result = await detector.detect()
        assert result == []


class TestToilDetectorEnabled:
    def test_enabled_by_default(self):
        detector = ToilDetector()
        assert detector.enabled is True

    @pytest.mark.asyncio
    async def test_run_exits_immediately_when_disabled(self):
        with patch.object(ToilDetector, "enabled", False):
            detector = ToilDetector()
            # run() should return immediately without sleeping
            await detector.run()
