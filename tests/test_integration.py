"""End-to-end integration tests.

These tests validate the full alert → diagnosis → approval flow
against real (or mocked) external services. They are skipped by default
unless the required environment variables are set.

Run with:
    ANTHROPIC_API_KEY=sk-ant-... pytest tests/test_integration.py -v

Markers:
    @pytest.mark.integration  — requires real Anthropic API key
    @pytest.mark.k8s          — requires a real Kubernetes cluster
"""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from k8s_runbook_agent.agent.orchestrator import (
    _parse_diagnosis_block,
    _parse_fix_block,
)
from k8s_runbook_agent.agent.session import DiagnosisSession, SessionPhase, session_store
from k8s_runbook_agent.agent.tool_registry import build_default_registry
from k8s_runbook_agent.config import settings
from k8s_runbook_agent.models import AlertStatus, GrafanaAlert
from k8s_runbook_agent.server import app

# Skip all tests if no API key
HAS_ANTHROPIC = bool(os.getenv("ANTHROPIC_API_KEY"))
def _check_k8s_connectivity() -> bool:
    """Check if we can actually connect to a K8s cluster."""
    try:
        from kubernetes import client, config
        config.load_kube_config()
        v1 = client.CoreV1Api()
        v1.list_namespace(limit=1, _request_timeout=3)
        return True
    except Exception:
        return False

HAS_K8S = _check_k8s_connectivity()

pytestmark = pytest.mark.integration


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestWebhookToSessionFlow:
    """Test: Grafana webhook → session creation → session appears in API."""

    @pytest.mark.asyncio
    async def test_webhook_creates_session(self, client, clean_session_store):
        payload = {
            "alerts": [{
                "status": "firing",
                "labels": {
                    "alertname": "IntegrationTestAlert",
                    "namespace": "test-ns",
                    "severity": "warning",
                },
                "annotations": {"summary": "Integration test alert"},
                "fingerprint": f"fp-integration-{id(client)}",
            }]
        }

        resp = await client.post("/webhooks/grafana", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["alerts_received"] == 1

        # Session should be visible in the sessions API
        sessions_resp = await client.get("/sessions")
        if sessions_resp.status_code == 200:
            sessions = sessions_resp.json()
            if "total" in sessions:
                assert sessions["total"] >= 1


class TestToolRegistryIntegrity:
    """Test: all 22 tools generate valid Anthropic API schemas."""

    def test_all_tools_have_valid_schemas(self):
        registry = build_default_registry()
        tools = registry.to_anthropic_tools()

        assert len(tools) == 22

        for tool in tools:
            # Anthropic requires these fields
            assert "name" in tool
            assert isinstance(tool["name"], str)
            assert len(tool["name"]) > 0

            assert "description" in tool
            assert isinstance(tool["description"], str)

            assert "input_schema" in tool
            schema = tool["input_schema"]
            assert schema["type"] == "object"
            assert "properties" in schema

    def test_no_duplicate_tool_names(self):
        registry = build_default_registry()
        tools = registry.to_anthropic_tools()
        names = [t["name"] for t in tools]
        assert len(names) == len(set(names)), f"Duplicate tool names: {names}"


class TestRunbookSearchIntegrity:
    """Test: every alert type maps to a runbook with correct schema."""

    ALERT_RUNBOOK_PAIRS = [
        ("KubePodCrashLooping", "pod-crashloopbackoff"),
        ("KubePodOOMKilled", "pod-oomkilled"),
        ("KubePodImagePullBackOff", "pod-imagepullbackoff"),
        ("KubeNodeNotReady", "node-notready"),
        ("KubeDeploymentReplicasMismatch", "deployment-failed-rollout"),
        ("KubeServiceWithNoEndpoints", "service-no-endpoints"),
        ("KubePersistentVolumeClaimPending", "pvc-pending"),
        ("CPUThrottlingHigh", "cpu-throttling"),
        ("KubeHpaMaxedOut", "hpa-maxed-out"),
        ("KubePodEvicted", "pod-evicted"),
        ("KubePodNotScheduled", "pod-unschedulable"),
        ("CoreDNSDown", "dns-resolution-failure"),
        ("KubeJobFailed", "job-failure"),
        ("HighErrorRate", "high-error-rate"),
        ("CertificateExpiringSoon", "certificate-expiry"),
        ("KubeIngressNoBackend", "ingress-misconfigured"),
    ]

    @pytest.mark.parametrize("alert_name,expected_runbook", ALERT_RUNBOOK_PAIRS)
    def test_alert_maps_to_runbook(self, alert_name, expected_runbook):
        from k8s_runbook_agent.knowledge.loader import RunbookStore

        store = RunbookStore()
        store.load_directory(settings.runbook_dir)

        matches = store.search(alert_name=alert_name, query=alert_name, labels={})
        assert len(matches) > 0, f"No match for {alert_name}"
        assert matches[0].runbook_id == expected_runbook

    def test_every_runbook_has_valid_structure(self):
        from k8s_runbook_agent.knowledge.loader import RunbookStore

        store = RunbookStore()
        store.load_directory(settings.runbook_dir)

        for rb in store.all_runbooks:
            assert rb.metadata.id
            assert rb.metadata.title
            assert len(rb.initial_inspection) >= 2, f"{rb.metadata.id}: needs >=2 inspection steps"
            assert len(rb.diagnosis_tree) >= 2, f"{rb.metadata.id}: needs >=2 diagnosis branches"
            assert rb.fallback.message
            assert rb.fallback.action

            for branch in rb.diagnosis_tree:
                assert branch.symptom
                assert len(branch.root_causes) >= 1

    def test_every_runbook_has_version(self):
        from k8s_runbook_agent.knowledge.loader import RunbookStore

        store = RunbookStore()
        store.load_directory(settings.runbook_dir)

        for rb in store.all_runbooks:
            version = store.get_version(rb.metadata.id)
            assert version is not None, f"{rb.metadata.id}: missing version"
            assert version.file_hash, f"{rb.metadata.id}: missing file_hash"
            assert version.loaded_at is not None


class TestGuardrailsIntegrity:
    """Test: guardrails catch all expected dangerous patterns."""

    def test_all_blocked_namespaces_are_enforced(self):
        from k8s_runbook_agent.agent.guardrails import BLOCKED_NAMESPACES, evaluate_guardrails
        from k8s_runbook_agent.models import Confidence, RiskLevel

        for ns in BLOCKED_NAMESPACES:
            alert = GrafanaAlert(
                alert_name="Test", status=AlertStatus.FIRING,
                labels={"namespace": ns},
            )
            session = DiagnosisSession(alert)
            session.set_diagnosis("test", Confidence.HIGH, ["e"])
            session.set_fix_proposal("fix", "desc", RiskLevel.LOW, "dry", "rollback")
            result = evaluate_guardrails(session)
            assert not result.passed, f"Namespace {ns} should be blocked"


@pytest.mark.skipif(not HAS_ANTHROPIC, reason="ANTHROPIC_API_KEY not set")
class TestAnthropicIntegration:
    """Live tests against the Anthropic API — requires real API key."""

    @pytest.mark.asyncio
    async def test_claude_can_generate_diagnosis(self):
        """Verify Claude produces parseable diagnosis/fix output."""
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system="You are a K8s diagnostic agent. Output a structured diagnosis.",
            messages=[{
                "role": "user",
                "content": (
                    "A pod named api-server-xyz in namespace production is in CrashLoopBackOff. "
                    "The last termination reason was OOMKilled with exit code 137. "
                    "Memory usage was 254Mi with a limit of 256Mi.\n\n"
                    "Provide your diagnosis in the required format:\n"
                    "```diagnosis\nROOT_CAUSE: ...\nCONFIDENCE: HIGH|MEDIUM|LOW\n"
                    "EVIDENCE:\n  - ...\nRULED_OUT:\n  - ...\n```\n\n"
                    "Then propose a fix:\n"
                    "```fix_proposal\nSUMMARY: ...\nRISK: LOW|MEDIUM|HIGH|CRITICAL\n"
                    "DESCRIPTION: |\n  ...\nROLLBACK: |\n  ...\n```"
                ),
            }],
        )

        text = response.content[0].text
        diag = _parse_diagnosis_block(text)
        assert diag is not None, f"Claude did not produce parseable diagnosis:\n{text[:500]}"
        assert "root_cause" in diag
        assert diag["confidence"] in ("HIGH", "MEDIUM", "LOW")

        fix = _parse_fix_block(text)
        assert fix is not None, f"Claude did not produce parseable fix:\n{text[:500]}"
        assert "summary" in fix


@pytest.mark.skipif(not HAS_K8S, reason="No kubeconfig found")
class TestKubernetesIntegration:
    """Live tests against a real K8s cluster."""

    @pytest.mark.asyncio
    async def test_list_namespaces(self):
        from k8s_runbook_agent.k8s_client import core_v1

        result = await core_v1.list_namespace(limit=5)
        assert len(result.items) > 0

    @pytest.mark.asyncio
    async def test_get_pod_status_tool(self):
        registry = build_default_registry()
        result = await registry.dispatch("list_resources", {
            "kind": "pod",
            "namespace": "kube-system",
        })
        assert not result.get("is_error", False)
        text = result["content"][0]["text"]
        assert "pod" in text.lower() or "found" in text.lower()

    @pytest.mark.asyncio
    async def test_get_node_conditions_tool(self):
        from k8s_runbook_agent.k8s_client import core_v1

        nodes = await core_v1.list_node(limit=1)
        if nodes.items:
            node_name = nodes.items[0].metadata.name
            registry = build_default_registry()
            result = await registry.dispatch("get_node_conditions", {
                "node_name": node_name,
            })
            assert not result.get("is_error", False)
