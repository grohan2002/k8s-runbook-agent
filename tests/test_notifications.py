"""Tests for the incident management notification system."""

import pytest

from k8s_runbook_agent.agent.session import DiagnosisSession
from k8s_runbook_agent.models import (
    AlertStatus,
    Confidence,
    GrafanaAlert,
    RiskLevel,
)
from k8s_runbook_agent.notifications.base import (
    IncidentContext,
    IncidentProvider,
    IncidentRouter,
    IncidentUrgency,
    session_to_urgency,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def diagnosed_session():
    alert = GrafanaAlert(
        alert_name="KubePodCrashLooping",
        status=AlertStatus.FIRING,
        labels={"namespace": "production", "pod": "api-abc123", "severity": "critical"},
        fingerprint="fp-test",
    )
    session = DiagnosisSession(alert)
    session.set_diagnosis("OOMKilled", Confidence.HIGH, ["exit code 137"])
    session.set_fix_proposal(
        summary="Increase memory to 512Mi",
        description="Patch deployment",
        risk_level=RiskLevel.LOW,
    )
    session.slack_channel = "C12345"
    session.slack_thread_ts = "1234567890.123456"
    return session


class FakeProvider(IncidentProvider):
    """In-memory provider for testing."""

    def __init__(self, name: str = "fake", enabled: bool = True):
        self._name = name
        self._enabled = enabled
        self.created: list[IncidentContext] = []
        self.acknowledged: list[str] = []
        self.resolved: list[tuple[str, str]] = []
        self.notes: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def create_incident(self, ctx: IncidentContext) -> str | None:
        self.created.append(ctx)
        return f"{self._name}-{ctx.session_id}"

    async def acknowledge_incident(self, incident_id: str) -> bool:
        self.acknowledged.append(incident_id)
        return True

    async def resolve_incident(self, incident_id: str, note: str = "") -> bool:
        self.resolved.append((incident_id, note))
        return True

    async def add_note(self, incident_id: str, note: str) -> bool:
        self.notes.append((incident_id, note))
        return True


# ---------------------------------------------------------------------------
# Tests: IncidentContext
# ---------------------------------------------------------------------------
class TestIncidentContext:
    def test_from_session(self, diagnosed_session):
        ctx = IncidentContext.from_session(diagnosed_session, "SLA breach!")
        assert "KubePodCrashLooping" in ctx.title
        assert "production" in ctx.title
        assert ctx.urgency == IncidentUrgency.CRITICAL
        assert ctx.alert_name == "KubePodCrashLooping"
        assert ctx.namespace == "production"
        assert ctx.pod == "api-abc123"
        assert ctx.fix_summary == "Increase memory to 512Mi"
        assert ctx.risk_level == "low"
        assert "SLA breach!" in ctx.description
        assert "OOMKilled" in ctx.description
        assert ctx.slack_thread_url  # Should be populated

    def test_slack_thread_url_format(self, diagnosed_session):
        ctx = IncidentContext.from_session(diagnosed_session)
        assert "C12345" in ctx.slack_thread_url
        assert "1234567890123456" in ctx.slack_thread_url  # dots removed

    def test_no_slack_thread(self, diagnosed_session):
        diagnosed_session.slack_channel = None
        ctx = IncidentContext.from_session(diagnosed_session)
        assert ctx.slack_thread_url == ""


class TestUrgencyMapping:
    def test_critical(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING,
            labels={"severity": "critical"},
        )
        session = DiagnosisSession(alert)
        assert session_to_urgency(session) == IncidentUrgency.CRITICAL

    def test_warning(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING,
            labels={"severity": "warning"},
        )
        session = DiagnosisSession(alert)
        assert session_to_urgency(session) == IncidentUrgency.HIGH

    def test_info(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING,
            labels={"severity": "info"},
        )
        session = DiagnosisSession(alert)
        assert session_to_urgency(session) == IncidentUrgency.LOW


# ---------------------------------------------------------------------------
# Tests: IncidentRouter
# ---------------------------------------------------------------------------
class TestIncidentRouter:
    @pytest.mark.asyncio
    async def test_create_fans_out(self, diagnosed_session):
        provider1 = FakeProvider("pd")
        provider2 = FakeProvider("og")
        router = IncidentRouter()
        router.register(provider1)
        router.register(provider2)

        ctx = IncidentContext.from_session(diagnosed_session)
        result = await router.create_incident(ctx)

        assert "pd" in result
        assert "og" in result
        assert len(provider1.created) == 1
        assert len(provider2.created) == 1

    @pytest.mark.asyncio
    async def test_acknowledge_all(self):
        provider1 = FakeProvider("pd")
        provider2 = FakeProvider("og")
        router = IncidentRouter()
        router.register(provider1)
        router.register(provider2)

        ids = {"pd": "pd-123", "og": "og-456"}
        await router.acknowledge_all(ids)

        assert "pd-123" in provider1.acknowledged
        assert "og-456" in provider2.acknowledged

    @pytest.mark.asyncio
    async def test_resolve_all(self):
        provider1 = FakeProvider("pd")
        router = IncidentRouter()
        router.register(provider1)

        await router.resolve_all({"pd": "pd-123"}, note="Fix applied")
        assert provider1.resolved == [("pd-123", "Fix applied")]

    @pytest.mark.asyncio
    async def test_add_note_all(self):
        provider1 = FakeProvider("pd")
        router = IncidentRouter()
        router.register(provider1)

        await router.add_note_all({"pd": "pd-123"}, "Execution started")
        assert provider1.notes == [("pd-123", "Execution started")]

    def test_skips_disabled_providers(self):
        enabled = FakeProvider("enabled", enabled=True)
        disabled = FakeProvider("disabled", enabled=False)
        router = IncidentRouter()
        router.register(enabled)
        router.register(disabled)

        assert router.enabled_providers == ["enabled"]
        assert router.has_providers is True

    def test_no_providers(self):
        router = IncidentRouter()
        assert router.has_providers is False
        assert router.enabled_providers == []

    @pytest.mark.asyncio
    async def test_tolerates_provider_failure(self, diagnosed_session):
        class FailingProvider(FakeProvider):
            async def create_incident(self, ctx):
                raise ConnectionError("API down")

        failing = FailingProvider("failing")
        working = FakeProvider("working")
        router = IncidentRouter()
        router.register(failing)
        router.register(working)

        ctx = IncidentContext.from_session(diagnosed_session)
        result = await router.create_incident(ctx)

        # Working provider should still succeed
        assert "working" in result
        assert "failing" not in result
        assert len(working.created) == 1


# ---------------------------------------------------------------------------
# Tests: PagerDuty provider (unit — no real API calls)
# ---------------------------------------------------------------------------
class TestPagerDutyProvider:
    def test_disabled_without_routing_key(self):
        from k8s_runbook_agent.notifications.pagerduty import PagerDutyProvider

        provider = PagerDutyProvider(routing_key="")
        assert not provider.enabled
        assert provider.name == "pagerduty"

    def test_enabled_with_routing_key(self):
        from k8s_runbook_agent.notifications.pagerduty import PagerDutyProvider

        provider = PagerDutyProvider(routing_key="test-key-123")
        assert provider.enabled

    def test_severity_mapping(self):
        from k8s_runbook_agent.notifications.pagerduty import PagerDutyProvider

        provider = PagerDutyProvider(routing_key="test")
        assert provider._map_severity(IncidentUrgency.CRITICAL) == "critical"
        assert provider._map_severity(IncidentUrgency.HIGH) == "error"
        assert provider._map_severity(IncidentUrgency.LOW) == "warning"


# ---------------------------------------------------------------------------
# Tests: OpsGenie provider (unit — no real API calls)
# ---------------------------------------------------------------------------
class TestOpsGenieProvider:
    def test_disabled_without_api_key(self):
        from k8s_runbook_agent.notifications.opsgenie import OpsGenieProvider

        provider = OpsGenieProvider(api_key="")
        assert not provider.enabled
        assert provider.name == "opsgenie"

    def test_enabled_with_api_key(self):
        from k8s_runbook_agent.notifications.opsgenie import OpsGenieProvider

        provider = OpsGenieProvider(api_key="test-key-123")
        assert provider.enabled

    def test_priority_mapping(self):
        from k8s_runbook_agent.notifications.opsgenie import OpsGenieProvider

        provider = OpsGenieProvider(api_key="test")
        assert provider._map_priority(IncidentUrgency.CRITICAL) == "P1"
        assert provider._map_priority(IncidentUrgency.HIGH) == "P2"
        assert provider._map_priority(IncidentUrgency.LOW) == "P3"

    def test_us_endpoint(self):
        from k8s_runbook_agent.notifications.opsgenie import OpsGenieProvider

        provider = OpsGenieProvider(api_key="test", region="us")
        assert "api.opsgenie.com" in provider._base_url

    def test_eu_endpoint(self):
        from k8s_runbook_agent.notifications.opsgenie import OpsGenieProvider

        provider = OpsGenieProvider(api_key="test", region="eu")
        assert "api.eu.opsgenie.com" in provider._base_url


# ---------------------------------------------------------------------------
# Tests: Session incident_ids field
# ---------------------------------------------------------------------------
class TestSessionIncidentIds:
    def test_default_empty(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={},
        )
        session = DiagnosisSession(alert)
        assert session.incident_ids == {}

    def test_store_and_retrieve(self):
        alert = GrafanaAlert(
            alert_name="Test", status=AlertStatus.FIRING, labels={},
        )
        session = DiagnosisSession(alert)
        session.incident_ids["pagerduty"] = "pd-123"
        session.incident_ids["opsgenie"] = "og-456"
        assert session.incident_ids["pagerduty"] == "pd-123"
        assert session.incident_ids["opsgenie"] == "og-456"
