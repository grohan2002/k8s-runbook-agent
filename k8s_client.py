"""Kubernetes client initialization.

Tries in-cluster config first, then falls back to kubeconfig file.
All tools import the pre-configured API instances from here.

IMPORTANT: The kubernetes-client library uses synchronous HTTP (urllib3).
All K8s API calls MUST go through the async proxy wrappers exported here:

    from ..k8s_client import core_v1, apps_v1, ...

    # In async tool handlers:
    pod = await core_v1.read_namespaced_pod(name="x", namespace="default")

The `AsyncK8sProxy` transparently runs every call in a thread pool so the
asyncio event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from kubernetes import client, config
from kubernetes.client import (
    AppsV1Api,
    AutoscalingV1Api,
    CoreV1Api,
    CustomObjectsApi,
    NetworkingV1Api,
)

from .config import settings


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def _load_config() -> bool:
    """Load Kubernetes configuration. Returns True if successful."""
    try:
        if settings.kubeconfig:
            config.load_kube_config(config_file=settings.kubeconfig)
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
        return True
    except Exception:
        return False


_k8s_configured = _load_config()


# ---------------------------------------------------------------------------
# Async proxy — wraps synchronous K8s client methods with asyncio.to_thread
# ---------------------------------------------------------------------------
class AsyncK8sProxy:
    """Wraps a synchronous kubernetes-client API object.

    Any method call is automatically dispatched to a thread pool via
    ``asyncio.to_thread``, so the asyncio event loop is never blocked.

    Usage:
        core_v1 = AsyncK8sProxy(CoreV1Api())
        pod = await core_v1.read_namespaced_pod(name="x", namespace="default")

    In non-async contexts (tests, scripts), access the underlying sync client:
        core_v1.sync.read_namespaced_pod(name="x", namespace="default")
    """

    def __init__(self, api: Any) -> None:
        object.__setattr__(self, "_api", api)

    @property
    def sync(self) -> Any:
        """Access the raw synchronous kubernetes-client API object."""
        return object.__getattribute__(self, "_api")

    def __getattr__(self, name: str) -> Any:
        api = object.__getattribute__(self, "_api")
        attr = getattr(api, name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(attr, *args, **kwargs)

        return _async_wrapper


# ---------------------------------------------------------------------------
# Pre-configured async API instances
# ---------------------------------------------------------------------------
core_v1: AsyncK8sProxy = AsyncK8sProxy(CoreV1Api())
apps_v1: AsyncK8sProxy = AsyncK8sProxy(AppsV1Api())
autoscaling_v1: AsyncK8sProxy = AsyncK8sProxy(AutoscalingV1Api())
networking_v1: AsyncK8sProxy = AsyncK8sProxy(NetworkingV1Api())
custom_objects: AsyncK8sProxy = AsyncK8sProxy(CustomObjectsApi())
