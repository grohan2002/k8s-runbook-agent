"""Tests for deterministic alert routing."""

import pytest

from k8s_runbook_agent.agent.multi_agent.routing import (
    route_alert,
    route_by_alert_name,
    route_by_labels,
)
from k8s_runbook_agent.agent.multi_agent.tool_subsets import SpecialistDomain
from k8s_runbook_agent.models import AlertStatus, GrafanaAlert


class TestRouteByAlertName:
    @pytest.mark.parametrize("alert_name,expected", [
        ("KubePodCrashLooping", SpecialistDomain.POD),
        ("KubePodOOMKilled", SpecialistDomain.POD),
        ("KubePodImagePullBackOff", SpecialistDomain.POD),
        ("KubePodEvicted", SpecialistDomain.POD),
        ("KubePodNotScheduled", SpecialistDomain.POD),
        ("PodPending", SpecialistDomain.POD),
        ("CoreDNSDown", SpecialistDomain.NETWORK),
        ("DNSResolutionFailure", SpecialistDomain.NETWORK),
        ("KubeIngressNoBackend", SpecialistDomain.NETWORK),
        ("KubeServiceWithNoEndpoints", SpecialistDomain.NETWORK),
        ("CertificateExpiringSoon", SpecialistDomain.NETWORK),
        ("TLSCertExpiry", SpecialistDomain.NETWORK),
        ("KubeNodeNotReady", SpecialistDomain.INFRASTRUCTURE),
        ("KubeNodeUnreachable", SpecialistDomain.INFRASTRUCTURE),
        ("CPUThrottlingHigh", SpecialistDomain.INFRASTRUCTURE),
        ("KubeHpaMaxedOut", SpecialistDomain.INFRASTRUCTURE),
        ("KubePersistentVolumeClaimPending", SpecialistDomain.INFRASTRUCTURE),
        ("DiskPressure", SpecialistDomain.INFRASTRUCTURE),
        ("HighErrorRate", SpecialistDomain.APPLICATION),
        ("HTTP5xxRateHigh", SpecialistDomain.APPLICATION),
        ("KubeDeploymentReplicasMismatch", SpecialistDomain.APPLICATION),
        ("KubeJobFailed", SpecialistDomain.APPLICATION),
    ])
    def test_known_alerts_route_correctly(self, alert_name, expected):
        result = route_by_alert_name(alert_name)
        assert result == expected, f"{alert_name} routed to {result}, expected {expected}"

    def test_unknown_alert_returns_none(self):
        assert route_by_alert_name("CompletelyUnknownAlert") is None


class TestRouteByLabels:
    def test_node_label_routes_to_infra(self):
        alert = GrafanaAlert(
            alert_name="Unknown", status=AlertStatus.FIRING,
            labels={"node": "worker-1"},
        )
        assert route_by_labels(alert) == SpecialistDomain.INFRASTRUCTURE

    def test_ingress_label_routes_to_network(self):
        alert = GrafanaAlert(
            alert_name="Unknown", status=AlertStatus.FIRING,
            labels={"ingress": "my-ingress"},
        )
        assert route_by_labels(alert) == SpecialistDomain.NETWORK

    def test_service_label_routes_to_network(self):
        alert = GrafanaAlert(
            alert_name="Unknown", status=AlertStatus.FIRING,
            labels={"service": "my-svc"},
        )
        assert route_by_labels(alert) == SpecialistDomain.NETWORK

    def test_pvc_label_routes_to_infra(self):
        alert = GrafanaAlert(
            alert_name="Unknown", status=AlertStatus.FIRING,
            labels={"persistentvolumeclaim": "data-pvc"},
        )
        assert route_by_labels(alert) == SpecialistDomain.INFRASTRUCTURE

    def test_pod_label_routes_to_pod(self):
        alert = GrafanaAlert(
            alert_name="Unknown", status=AlertStatus.FIRING,
            labels={"pod": "api-server-xyz"},
        )
        assert route_by_labels(alert) == SpecialistDomain.POD

    def test_no_labels_defaults_to_application(self):
        alert = GrafanaAlert(
            alert_name="Unknown", status=AlertStatus.FIRING,
            labels={},
        )
        assert route_by_labels(alert) == SpecialistDomain.APPLICATION

    def test_node_with_pod_still_routes_to_infra(self):
        """Node label takes priority when no pod is present."""
        alert = GrafanaAlert(
            alert_name="Unknown", status=AlertStatus.FIRING,
            labels={"node": "worker-1", "instance": "10.0.0.1"},
        )
        assert route_by_labels(alert) == SpecialistDomain.INFRASTRUCTURE


class TestRouteAlert:
    def test_alert_name_takes_priority(self):
        alert = GrafanaAlert(
            alert_name="KubePodCrashLooping", status=AlertStatus.FIRING,
            labels={"node": "worker-1"},  # Would route to INFRA by labels
        )
        # But alert name matches POD
        assert route_alert(alert) == SpecialistDomain.POD

    def test_falls_through_to_labels(self):
        alert = GrafanaAlert(
            alert_name="CustomMetricAlert", status=AlertStatus.FIRING,
            labels={"pod": "api-xyz"},
        )
        assert route_alert(alert) == SpecialistDomain.POD
