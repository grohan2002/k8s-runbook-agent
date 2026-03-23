"""Multi-cluster support — diagnose across multiple Kubernetes clusters.

Allows the agent to inspect resources in different clusters based on
alert labels (cluster, cluster_name, or datasource).

Cluster registry is loaded from config:
  CLUSTER_CONFIGS='[
    {"name": "prod-us-east", "kubeconfig": "/secrets/kubeconfig-prod-east"},
    {"name": "prod-us-west", "kubeconfig": "/secrets/kubeconfig-prod-west"},
    {"name": "staging", "kubeconfig": "/secrets/kubeconfig-staging"}
  ]'

If no CLUSTER_CONFIGS is set, the agent uses the default single-cluster
configuration from KUBECONFIG or in-cluster config.

Tools automatically receive a `cluster` parameter when multi-cluster
is enabled. The agent picks the cluster from alert labels.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from kubernetes import client, config
from kubernetes.client import (
    AppsV1Api,
    AutoscalingV1Api,
    CoreV1Api,
    CustomObjectsApi,
    NetworkingV1Api,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cluster definition
# ---------------------------------------------------------------------------
@dataclass
class ClusterConfig:
    """A single Kubernetes cluster connection."""

    name: str
    kubeconfig: str = ""  # Path to kubeconfig file
    context: str = ""  # Specific context within the kubeconfig
    display_name: str = ""  # Human-readable name (defaults to name)
    environment: str = ""  # prod, staging, dev
    region: str = ""  # us-east-1, eu-west-1, etc.
    api_server: str = ""  # For display only (extracted from kubeconfig)
    is_default: bool = False

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.name


@dataclass
class ClusterAPIs:
    """Pre-loaded K8s API instances for a cluster."""

    core_v1: CoreV1Api
    apps_v1: AppsV1Api
    autoscaling_v1: AutoscalingV1Api
    networking_v1: NetworkingV1Api
    custom_objects: CustomObjectsApi


# ---------------------------------------------------------------------------
# Cluster registry
# ---------------------------------------------------------------------------
class ClusterRegistry:
    """Manages connections to multiple Kubernetes clusters.

    Usage:
        registry = ClusterRegistry()
        registry.load_from_json(os.environ.get("CLUSTER_CONFIGS", ""))

        # Get APIs for a specific cluster
        apis = registry.get_apis("prod-us-east")
        pods = apis.core_v1.list_namespaced_pod(namespace="default")

        # Resolve cluster from alert labels
        cluster_name = registry.resolve_cluster(alert.labels)
        apis = registry.get_apis(cluster_name)
    """

    def __init__(self) -> None:
        self._clusters: dict[str, ClusterConfig] = {}
        self._apis: dict[str, ClusterAPIs] = {}
        self._default_cluster: str | None = None

    def load_from_json(self, config_json: str) -> int:
        """Load cluster configs from a JSON string (env variable).

        Returns number of clusters loaded.
        """
        if not config_json:
            return 0

        try:
            configs = json.loads(config_json)
        except json.JSONDecodeError:
            logger.error("Invalid CLUSTER_CONFIGS JSON: %s", config_json[:100])
            return 0

        count = 0
        for entry in configs:
            try:
                cluster = ClusterConfig(**entry)
                self.register(cluster)
                count += 1
            except Exception:
                logger.exception("Failed to load cluster config: %s", entry)

        logger.info("Loaded %d cluster configurations", count)
        return count

    def register(self, cluster: ClusterConfig) -> None:
        """Register a cluster and initialize its K8s API clients."""
        self._clusters[cluster.name] = cluster

        try:
            apis = self._create_apis(cluster)
            self._apis[cluster.name] = apis
            logger.info(
                "Registered cluster '%s' (env=%s, region=%s)",
                cluster.name, cluster.environment, cluster.region,
            )

            if cluster.is_default:
                self._default_cluster = cluster.name

        except Exception:
            logger.exception("Failed to create K8s APIs for cluster '%s'", cluster.name)

    def _create_apis(self, cluster: ClusterConfig) -> ClusterAPIs:
        """Create K8s API instances for a cluster."""
        api_client = self._load_api_client(cluster)
        return ClusterAPIs(
            core_v1=CoreV1Api(api_client),
            apps_v1=AppsV1Api(api_client),
            autoscaling_v1=AutoscalingV1Api(api_client),
            networking_v1=NetworkingV1Api(api_client),
            custom_objects=CustomObjectsApi(api_client),
        )

    def _load_api_client(self, cluster: ClusterConfig) -> client.ApiClient:
        """Load a K8s ApiClient for a specific cluster."""
        if cluster.kubeconfig:
            kwargs: dict[str, Any] = {"config_file": cluster.kubeconfig}
            if cluster.context:
                kwargs["context"] = cluster.context
            return config.new_client_from_config(**kwargs)
        else:
            # Assume in-cluster
            configuration = client.Configuration()
            config.load_incluster_config(client_configuration=configuration)
            return client.ApiClient(configuration)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def get_apis(self, cluster_name: str | None = None) -> ClusterAPIs | None:
        """Get K8s API instances for a cluster. Falls back to default."""
        name = cluster_name or self._default_cluster
        if not name:
            return None
        return self._apis.get(name)

    def get_config(self, cluster_name: str) -> ClusterConfig | None:
        return self._clusters.get(cluster_name)

    @property
    def cluster_names(self) -> list[str]:
        return list(self._clusters.keys())

    @property
    def is_multi_cluster(self) -> bool:
        return len(self._clusters) > 1

    @property
    def default_cluster(self) -> str | None:
        return self._default_cluster

    # ------------------------------------------------------------------
    # Cluster resolution from alert labels
    # ------------------------------------------------------------------
    def resolve_cluster(self, labels: dict[str, str]) -> str | None:
        """Determine which cluster an alert belongs to from its labels.

        Checks these labels in order:
          1. cluster
          2. cluster_name
          3. datasource
          4. Partial name matching against registered cluster names
        """
        # Direct label match
        for label_key in ("cluster", "cluster_name", "datasource"):
            value = labels.get(label_key, "")
            if value and value in self._clusters:
                return value

        # Partial match — "prod-us-east-1" might match cluster "prod-us-east"
        for label_key in ("cluster", "cluster_name"):
            value = labels.get(label_key, "")
            if value:
                for name in self._clusters:
                    if name in value or value in name:
                        return name

        # Environment-based fallback
        env = labels.get("environment", labels.get("env", ""))
        if env:
            for name, cfg in self._clusters.items():
                if cfg.environment == env:
                    return name

        return self._default_cluster

    def summary(self) -> str:
        """Human-readable summary of registered clusters."""
        if not self._clusters:
            return "No clusters configured (single-cluster mode)"

        lines = [f"Registered clusters ({len(self._clusters)}):"]
        for name, cfg in self._clusters.items():
            default_tag = " (default)" if name == self._default_cluster else ""
            lines.append(
                f"  • {cfg.display_name}{default_tag} "
                f"[env={cfg.environment}, region={cfg.region}]"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
cluster_registry = ClusterRegistry()
