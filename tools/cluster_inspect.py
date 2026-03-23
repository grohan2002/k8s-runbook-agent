"""Read-only Kubernetes cluster inspection tools.

All tools in this module are safe to auto-approve — they only read cluster state.
They return structured text that Claude can reason over during diagnosis.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .decorator import tool
from kubernetes.client.rest import ApiException

from ..k8s_client import apps_v1, autoscaling_v1, core_v1, custom_objects, networking_v1


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"ERROR: {text}"}], "is_error": True}


def _api_error(e: ApiException, resource: str) -> dict[str, Any]:
    if e.status == 404:
        return _error(f"{resource} not found.")
    return _error(f"Kubernetes API error ({e.status}): {e.reason}")


def _age(ts) -> str:
    """Human-readable age from a k8s timestamp."""
    if ts is None:
        return "unknown"
    if isinstance(ts, str):
        return ts
    now = datetime.now(timezone.utc)
    delta = now - ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else now - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600}h"


# ------------------------------------------------------------------
# Tool 1: get_pod_status
# ------------------------------------------------------------------
@tool(
    "get_pod_status",
    "Get the status of one or more pods including phase, conditions, container "
    "statuses, restart counts, and termination reasons. Pass a pod name for a "
    "specific pod, or a label_selector to match multiple pods.",
    {"namespace": str, "pod_name": str, "label_selector": str},
)
async def get_pod_status(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    pod_name = args.get("pod_name", "")
    label_selector = args.get("label_selector", "")

    try:
        if pod_name:
            pods = [await core_v1.read_namespaced_pod(name=pod_name, namespace=ns)]
        elif label_selector:
            result = await core_v1.list_namespaced_pod(namespace=ns, label_selector=label_selector)
            pods = result.items
        else:
            return _error("Provide either pod_name or label_selector.")
    except ApiException as e:
        return _api_error(e, f"Pod {pod_name or label_selector} in {ns}")

    if not pods:
        return _text(f"No pods found matching selector '{label_selector}' in namespace '{ns}'.")

    lines: list[str] = []
    for pod in pods:
        status = pod.status
        spec = pod.spec
        lines.append(f"Pod: {pod.metadata.namespace}/{pod.metadata.name}")
        lines.append(f"  Phase: {status.phase}")
        lines.append(f"  Node: {spec.node_name or 'unscheduled'}")
        lines.append(f"  Age: {_age(pod.metadata.creation_timestamp)}")
        lines.append(f"  QoS Class: {status.qos_class}")

        # Conditions
        if status.conditions:
            lines.append("  Conditions:")
            for c in status.conditions:
                lines.append(f"    {c.type}: {c.status}" + (f" (reason: {c.reason})" if c.reason else ""))

        # Container statuses
        for cs in status.container_statuses or []:
            lines.append(f"  Container: {cs.name}")
            lines.append(f"    Ready: {cs.ready}, Restarts: {cs.restart_count}")
            lines.append(f"    Image: {cs.image}")

            if cs.state:
                if cs.state.running:
                    lines.append(f"    State: Running (since {_age(cs.state.running.started_at)})")
                elif cs.state.waiting:
                    w = cs.state.waiting
                    lines.append(f"    State: Waiting (reason: {w.reason})")
                    if w.message:
                        lines.append(f"    Message: {w.message}")
                elif cs.state.terminated:
                    t = cs.state.terminated
                    lines.append(f"    State: Terminated (reason: {t.reason}, exitCode: {t.exit_code})")
                    if t.message:
                        lines.append(f"    Message: {t.message}")

            if cs.last_state and cs.last_state.terminated:
                t = cs.last_state.terminated
                lines.append(f"    Last Termination: reason={t.reason}, exitCode={t.exit_code}, at={t.finished_at}")

        # Init container statuses
        for cs in status.init_container_statuses or []:
            lines.append(f"  Init Container: {cs.name}")
            lines.append(f"    Ready: {cs.ready}, Restarts: {cs.restart_count}")
            if cs.state and cs.state.waiting:
                lines.append(f"    State: Waiting (reason: {cs.state.waiting.reason})")
            elif cs.state and cs.state.terminated:
                t = cs.state.terminated
                lines.append(f"    State: Terminated (reason: {t.reason}, exitCode: {t.exit_code})")

        # Resource requests and limits from spec
        for container in spec.containers:
            res = container.resources
            if res:
                lines.append(f"  Resources for {container.name}:")
                if res.requests:
                    lines.append(f"    Requests: {dict(res.requests)}")
                if res.limits:
                    lines.append(f"    Limits: {dict(res.limits)}")

        lines.append("")

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 2: get_pod_logs
# ------------------------------------------------------------------
@tool(
    "get_pod_logs",
    "Get logs from a pod's container. Set previous=true to get logs from the "
    "previous (crashed) container instance. Defaults to tail 100 lines.",
    {"namespace": str, "pod_name": str, "container": str, "previous": str, "tail_lines": str},
)
async def get_pod_logs(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    pod_name = args.get("pod_name", "")
    container = args.get("container", "") or None
    previous = args.get("previous", "false").lower() == "true"
    tail_lines = int(args.get("tail_lines", "100"))

    if not pod_name:
        return _error("pod_name is required.")

    try:
        logs = await core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=ns,
            container=container,
            previous=previous,
            tail_lines=tail_lines,
        )
    except ApiException as e:
        if e.status == 400 and "previous terminated" in str(e.body).lower():
            return _text(f"No previous container logs available for {pod_name}.")
        return _api_error(e, f"Logs for {pod_name}")

    if not logs:
        label = "previous " if previous else ""
        return _text(f"No {label}logs available for pod {ns}/{pod_name}.")

    header = f"--- Logs: {ns}/{pod_name}"
    if container:
        header += f" (container: {container})"
    if previous:
        header += " [PREVIOUS]"
    header += f" (last {tail_lines} lines) ---"

    return _text(f"{header}\n{logs}")


# ------------------------------------------------------------------
# Tool 3: get_events
# ------------------------------------------------------------------
@tool(
    "get_events",
    "Get Kubernetes events in a namespace, optionally filtered by involved object. "
    "Useful for finding scheduling failures, image pull errors, probe failures, etc.",
    {"namespace": str, "object_name": str, "object_kind": str, "since_minutes": str},
)
async def get_events(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    object_name = args.get("object_name", "")
    object_kind = args.get("object_kind", "")
    since_minutes = int(args.get("since_minutes", "60"))

    try:
        result = await core_v1.list_namespaced_event(namespace=ns)
    except ApiException as e:
        return _api_error(e, f"Events in {ns}")

    events = result.items
    cutoff = datetime.now(timezone.utc).timestamp() - (since_minutes * 60)

    filtered = []
    for ev in events:
        # Time filter
        ev_time = ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
        if ev_time:
            ts = ev_time.replace(tzinfo=timezone.utc) if ev_time.tzinfo is None else ev_time
            if ts.timestamp() < cutoff:
                continue

        # Object filter
        if object_name and ev.involved_object.name != object_name:
            continue
        if object_kind and ev.involved_object.kind.lower() != object_kind.lower():
            continue

        filtered.append(ev)

    if not filtered:
        return _text(f"No events found in {ns} (last {since_minutes}m).")

    # Sort by time descending
    filtered.sort(
        key=lambda e: (e.last_timestamp or e.event_time or e.metadata.creation_timestamp).timestamp(),
        reverse=True,
    )

    lines = [f"Events in {ns} (last {since_minutes}m): {len(filtered)} events\n"]
    for ev in filtered[:50]:  # Cap at 50 events
        ev_time = ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
        count = ev.count or 1
        lines.append(
            f"  [{_age(ev_time)} ago] [{ev.type}] {ev.involved_object.kind}/{ev.involved_object.name}: "
            f"{ev.reason} — {ev.message}"
            + (f" (x{count})" if count > 1 else "")
        )

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 4: get_resource_usage
# ------------------------------------------------------------------
@tool(
    "get_resource_usage",
    "Get CPU and memory usage from metrics-server for a pod or node. "
    "Requires metrics-server to be installed in the cluster.",
    {"resource_type": str, "namespace": str, "name": str},
)
async def get_resource_usage(args: dict[str, Any]) -> dict[str, Any]:
    resource_type = args.get("resource_type", "pod")
    ns = args.get("namespace", "default")
    name = args.get("name", "")

    try:
        if resource_type == "pod":
            if name:
                metrics = await custom_objects.get_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=ns,
                    plural="pods",
                    name=name,
                )
                pods_metrics = [metrics]
            else:
                result = await custom_objects.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=ns,
                    plural="pods",
                )
                pods_metrics = result.get("items", [])

            lines = []
            for pm in pods_metrics:
                pod_name = pm["metadata"]["name"]
                lines.append(f"Pod: {ns}/{pod_name}")
                for container in pm.get("containers", []):
                    cpu = container["usage"].get("cpu", "0")
                    mem = container["usage"].get("memory", "0")
                    lines.append(f"  Container {container['name']}: CPU={cpu}, Memory={mem}")
                lines.append("")

            if not lines:
                return _text(f"No pod metrics found in {ns}.")
            return _text("\n".join(lines))

        elif resource_type == "node":
            if name:
                metrics = await custom_objects.get_cluster_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="nodes",
                    name=name,
                )
                nodes_metrics = [metrics]
            else:
                result = await custom_objects.list_cluster_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="nodes",
                )
                nodes_metrics = result.get("items", [])

            lines = []
            for nm in nodes_metrics:
                node_name = nm["metadata"]["name"]
                cpu = nm["usage"].get("cpu", "0")
                mem = nm["usage"].get("memory", "0")
                lines.append(f"Node: {node_name}: CPU={cpu}, Memory={mem}")

            if not lines:
                return _text("No node metrics found.")
            return _text("\n".join(lines))

        else:
            return _error("resource_type must be 'pod' or 'node'.")

    except ApiException as e:
        if e.status == 404:
            return _error(
                "Metrics not available. Ensure metrics-server is installed: "
                "kubectl get deployment metrics-server -n kube-system"
            )
        return _api_error(e, f"Metrics for {resource_type}/{name}")


# ------------------------------------------------------------------
# Tool 5: describe_resource
# ------------------------------------------------------------------
@tool(
    "describe_resource",
    "Get a detailed description of any Kubernetes resource (similar to kubectl describe). "
    "Supports: pod, deployment, service, configmap, secret, node, pvc, ingress, hpa.",
    {"kind": str, "namespace": str, "name": str},
)
async def describe_resource(args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind", "").lower()
    ns = args.get("namespace", "default")
    name = args.get("name", "")

    if not name:
        return _error("name is required.")

    try:
        if kind == "pod":
            obj = await core_v1.read_namespaced_pod(name=name, namespace=ns)
        elif kind == "deployment":
            obj = await apps_v1.read_namespaced_deployment(name=name, namespace=ns)
        elif kind in ("service", "svc"):
            obj = await core_v1.read_namespaced_service(name=name, namespace=ns)
        elif kind in ("configmap", "cm"):
            obj = await core_v1.read_namespaced_config_map(name=name, namespace=ns)
        elif kind == "secret":
            obj = await core_v1.read_namespaced_secret(name=name, namespace=ns)
            # Redact secret data
            if obj.data:
                obj.data = {k: "<REDACTED>" for k in obj.data}
        elif kind == "node":
            obj = await core_v1.read_node(name=name)
        elif kind in ("pvc", "persistentvolumeclaim"):
            obj = await core_v1.read_namespaced_persistent_volume_claim(name=name, namespace=ns)
        elif kind == "ingress":
            obj = await networking_v1.read_namespaced_ingress(name=name, namespace=ns)
        elif kind in ("hpa", "horizontalpodautoscaler"):
            obj = await autoscaling_v1.read_namespaced_horizontal_pod_autoscaler(name=name, namespace=ns)
        elif kind in ("replicaset", "rs"):
            obj = await apps_v1.read_namespaced_replica_set(name=name, namespace=ns)
        elif kind in ("statefulset", "sts"):
            obj = await apps_v1.read_namespaced_stateful_set(name=name, namespace=ns)
        elif kind in ("daemonset", "ds"):
            obj = await apps_v1.read_namespaced_daemon_set(name=name, namespace=ns)
        else:
            return _error(
                f"Unsupported kind '{kind}'. Supported: pod, deployment, service, "
                "configmap, secret, node, pvc, ingress, hpa, replicaset, statefulset, daemonset."
            )
    except ApiException as e:
        return _api_error(e, f"{kind}/{name} in {ns}")

    # Convert to dict and format as readable text
    from kubernetes.client import ApiClient

    obj_dict = ApiClient().sanitize_for_serialization(obj)
    return _text(_format_describe(kind, obj_dict))


def _format_describe(kind: str, obj: dict) -> str:
    """Format a resource dict into a readable describe-style output."""
    meta = obj.get("metadata", {})
    lines = [
        f"Kind: {kind}",
        f"Name: {meta.get('name', 'unknown')}",
        f"Namespace: {meta.get('namespace', 'N/A')}",
        f"Created: {meta.get('creationTimestamp', 'unknown')}",
    ]

    labels = meta.get("labels", {})
    if labels:
        lines.append("Labels:")
        for k, v in labels.items():
            lines.append(f"  {k}: {v}")

    annotations = meta.get("annotations", {})
    if annotations:
        lines.append("Annotations:")
        for k, v in annotations.items():
            lines.append(f"  {k}: {v[:100]}{'...' if len(str(v)) > 100 else ''}")

    spec = obj.get("spec", {})
    if spec:
        lines.append("\nSpec:")
        lines.append(_indent_dict(spec, depth=1))

    status = obj.get("status", {})
    if status:
        lines.append("\nStatus:")
        lines.append(_indent_dict(status, depth=1))

    return "\n".join(lines)


def _indent_dict(d: Any, depth: int = 0, max_depth: int = 4) -> str:
    """Recursively format a dict with indentation, capped at max_depth."""
    indent = "  " * depth
    if depth >= max_depth:
        return f"{indent}..."

    if isinstance(d, dict):
        lines = []
        for k, v in d.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{indent}{k}:")
                lines.append(_indent_dict(v, depth + 1, max_depth))
            else:
                val_str = str(v)
                if len(val_str) > 200:
                    val_str = val_str[:200] + "..."
                lines.append(f"{indent}{k}: {val_str}")
        return "\n".join(lines)

    elif isinstance(d, list):
        if not d:
            return f"{indent}(empty)"
        lines = []
        for item in d[:20]:  # Cap list items
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}-")
                lines.append(_indent_dict(item, depth + 1, max_depth))
            else:
                lines.append(f"{indent}- {item}")
        if len(d) > 20:
            lines.append(f"{indent}... ({len(d) - 20} more items)")
        return "\n".join(lines)

    return f"{indent}{d}"


# ------------------------------------------------------------------
# Tool 6: get_resource_yaml
# ------------------------------------------------------------------
@tool(
    "get_resource_yaml",
    "Get the full YAML spec of a Kubernetes resource. Useful for reading exact "
    "field values like resource limits, probe configs, env vars, and volume mounts.",
    {"kind": str, "namespace": str, "name": str},
)
async def get_resource_yaml(args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind", "").lower()
    ns = args.get("namespace", "default")
    name = args.get("name", "")

    if not name:
        return _error("name is required.")

    try:
        if kind == "pod":
            obj = await core_v1.read_namespaced_pod(name=name, namespace=ns)
        elif kind == "deployment":
            obj = await apps_v1.read_namespaced_deployment(name=name, namespace=ns)
        elif kind in ("service", "svc"):
            obj = await core_v1.read_namespaced_service(name=name, namespace=ns)
        elif kind in ("configmap", "cm"):
            obj = await core_v1.read_namespaced_config_map(name=name, namespace=ns)
        elif kind == "secret":
            obj = await core_v1.read_namespaced_secret(name=name, namespace=ns)
            if obj.data:
                obj.data = {k: "<REDACTED>" for k in obj.data}
        elif kind in ("pvc", "persistentvolumeclaim"):
            obj = await core_v1.read_namespaced_persistent_volume_claim(name=name, namespace=ns)
        elif kind in ("statefulset", "sts"):
            obj = await apps_v1.read_namespaced_stateful_set(name=name, namespace=ns)
        elif kind in ("daemonset", "ds"):
            obj = await apps_v1.read_namespaced_daemon_set(name=name, namespace=ns)
        elif kind in ("replicaset", "rs"):
            obj = await apps_v1.read_namespaced_replica_set(name=name, namespace=ns)
        elif kind == "node":
            obj = await core_v1.read_node(name=name)
        else:
            return _error(
                f"Unsupported kind '{kind}'. Supported: pod, deployment, service, "
                "configmap, secret, pvc, statefulset, daemonset, replicaset, node."
            )
    except ApiException as e:
        return _api_error(e, f"{kind}/{name} in {ns}")

    import yaml
    from kubernetes.client import ApiClient

    obj_dict = ApiClient().sanitize_for_serialization(obj)

    # Remove noisy managed-fields metadata
    if "metadata" in obj_dict:
        obj_dict["metadata"].pop("managedFields", None)

    yaml_str = yaml.dump(obj_dict, default_flow_style=False, sort_keys=False)
    return _text(f"--- {kind}/{name} in {ns} ---\n{yaml_str}")


# ------------------------------------------------------------------
# Tool 7: list_resources
# ------------------------------------------------------------------
@tool(
    "list_resources",
    "List Kubernetes resources of a given kind in a namespace. "
    "Returns names, status, and key details. Supports label and field selectors.",
    {"kind": str, "namespace": str, "label_selector": str, "field_selector": str},
)
async def list_resources(args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind", "").lower()
    ns = args.get("namespace", "default")
    label_selector = args.get("label_selector", "")
    field_selector = args.get("field_selector", "")

    kwargs: dict[str, Any] = {}
    if label_selector:
        kwargs["label_selector"] = label_selector
    if field_selector:
        kwargs["field_selector"] = field_selector

    try:
        if kind in ("pod", "pods"):
            result = await core_v1.list_namespaced_pod(namespace=ns, **kwargs)
            items = [
                f"  {p.metadata.name}  Phase={p.status.phase}  "
                f"Restarts={sum(cs.restart_count for cs in (p.status.container_statuses or []))}  "
                f"Age={_age(p.metadata.creation_timestamp)}"
                for p in result.items
            ]
        elif kind in ("deployment", "deployments"):
            result = await apps_v1.list_namespaced_deployment(namespace=ns, **kwargs)
            items = [
                f"  {d.metadata.name}  Ready={d.status.ready_replicas or 0}/{d.spec.replicas}  "
                f"Age={_age(d.metadata.creation_timestamp)}"
                for d in result.items
            ]
        elif kind in ("service", "services", "svc"):
            result = await core_v1.list_namespaced_service(namespace=ns, **kwargs)
            items = [
                f"  {s.metadata.name}  Type={s.spec.type}  ClusterIP={s.spec.cluster_ip}  "
                f"Ports={','.join(str(p.port) for p in (s.spec.ports or []))}"
                for s in result.items
            ]
        elif kind in ("configmap", "configmaps", "cm"):
            result = await core_v1.list_namespaced_config_map(namespace=ns, **kwargs)
            items = [
                f"  {c.metadata.name}  Keys={list(c.data.keys()) if c.data else '(empty)'}  "
                f"Age={_age(c.metadata.creation_timestamp)}"
                for c in result.items
            ]
        elif kind in ("secret", "secrets"):
            result = await core_v1.list_namespaced_secret(namespace=ns, **kwargs)
            items = [
                f"  {s.metadata.name}  Type={s.type}  "
                f"Keys={list(s.data.keys()) if s.data else '(empty)'}"
                for s in result.items
            ]
        elif kind in ("event", "events"):
            result = await core_v1.list_namespaced_event(namespace=ns, **kwargs)
            items = [
                f"  [{e.type}] {e.involved_object.kind}/{e.involved_object.name}: "
                f"{e.reason} — {e.message}"
                for e in result.items[:30]
            ]
        elif kind in ("node", "nodes"):
            result = await core_v1.list_node(**kwargs)
            items = []
            for n in result.items:
                conditions = {c.type: c.status for c in (n.status.conditions or [])}
                ready = conditions.get("Ready", "Unknown")
                items.append(
                    f"  {n.metadata.name}  Ready={ready}  "
                    f"Age={_age(n.metadata.creation_timestamp)}"
                )
        elif kind in ("pvc", "persistentvolumeclaim", "persistentvolumeclaims"):
            result = await core_v1.list_namespaced_persistent_volume_claim(namespace=ns, **kwargs)
            items = [
                f"  {p.metadata.name}  Phase={p.status.phase}  "
                f"Capacity={p.status.capacity.get('storage', '?') if p.status.capacity else '?'}  "
                f"StorageClass={p.spec.storage_class_name}"
                for p in result.items
            ]
        elif kind in ("ingress", "ingresses"):
            result = await networking_v1.list_namespaced_ingress(namespace=ns, **kwargs)
            items = [
                f"  {i.metadata.name}  "
                f"Hosts={','.join(r.host or '*' for r in (i.spec.rules or []))}"
                for i in result.items
            ]
        else:
            return _error(
                f"Unsupported kind '{kind}'. Supported: pod, deployment, service, "
                "configmap, secret, event, node, pvc, ingress."
            )
    except ApiException as e:
        return _api_error(e, f"{kind} in {ns}")

    if not items:
        return _text(f"No {kind} found in {ns}.")

    header = f"{kind} in {ns}: {len(items)} found"
    return _text(header + "\n" + "\n".join(items))


# ------------------------------------------------------------------
# Tool 8: check_resource_exists
# ------------------------------------------------------------------
@tool(
    "check_resource_exists",
    "Check whether a specific Kubernetes resource exists. Returns true/false "
    "with details. Useful for verifying ConfigMaps, Secrets, Services exist.",
    {"kind": str, "namespace": str, "name": str},
)
async def check_resource_exists(args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind", "").lower()
    ns = args.get("namespace", "default")
    name = args.get("name", "")

    if not name:
        return _error("name is required.")

    try:
        if kind in ("configmap", "cm"):
            await core_v1.read_namespaced_config_map(name=name, namespace=ns)
        elif kind == "secret":
            await core_v1.read_namespaced_secret(name=name, namespace=ns)
        elif kind in ("service", "svc"):
            await core_v1.read_namespaced_service(name=name, namespace=ns)
        elif kind in ("pod", "pods"):
            await core_v1.read_namespaced_pod(name=name, namespace=ns)
        elif kind == "deployment":
            await apps_v1.read_namespaced_deployment(name=name, namespace=ns)
        elif kind in ("pvc", "persistentvolumeclaim"):
            await core_v1.read_namespaced_persistent_volume_claim(name=name, namespace=ns)
        elif kind in ("serviceaccount", "sa"):
            await core_v1.read_namespaced_service_account(name=name, namespace=ns)
        elif kind == "namespace":
            await core_v1.read_namespace(name=name)
        else:
            return _error(
                f"Unsupported kind '{kind}'. Supported: configmap, secret, service, "
                "pod, deployment, pvc, serviceaccount, namespace."
            )
        return _text(f"EXISTS: {kind}/{name} exists in namespace '{ns}'.")
    except ApiException as e:
        if e.status == 404:
            return _text(f"NOT FOUND: {kind}/{name} does not exist in namespace '{ns}'.")
        return _api_error(e, f"{kind}/{name}")


# ------------------------------------------------------------------
# Tool 9: get_endpoint_status
# ------------------------------------------------------------------
@tool(
    "get_endpoint_status",
    "Check whether a Kubernetes Service has ready endpoints (backing pods). "
    "Useful for diagnosing 'connection refused' when a dependency is down.",
    {"namespace": str, "service_name": str},
)
async def get_endpoint_status(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    service_name = args.get("service_name", "")

    if not service_name:
        return _error("service_name is required.")

    try:
        endpoints = await core_v1.read_namespaced_endpoints(name=service_name, namespace=ns)
    except ApiException as e:
        return _api_error(e, f"Endpoints for service {service_name}")

    subsets = endpoints.subsets or []
    if not subsets:
        return _text(
            f"Service {ns}/{service_name} has NO ready endpoints.\n"
            "This means no pods are matching the service selector or all matching pods are not ready."
        )

    lines = [f"Endpoints for service {ns}/{service_name}:"]
    total_ready = 0
    total_not_ready = 0

    for subset in subsets:
        ready = subset.addresses or []
        not_ready = subset.not_ready_addresses or []
        total_ready += len(ready)
        total_not_ready += len(not_ready)

        ports = subset.ports or []
        port_str = ", ".join(f"{p.name or 'unnamed'}:{p.port}/{p.protocol}" for p in ports)

        for addr in ready:
            target = addr.target_ref
            pod_ref = f" (pod: {target.name})" if target else ""
            lines.append(f"  READY: {addr.ip}{pod_ref}  Ports: {port_str}")

        for addr in not_ready:
            target = addr.target_ref
            pod_ref = f" (pod: {target.name})" if target else ""
            lines.append(f"  NOT READY: {addr.ip}{pod_ref}")

    lines.insert(1, f"  Total: {total_ready} ready, {total_not_ready} not ready")
    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 10: get_node_conditions
# ------------------------------------------------------------------
@tool(
    "get_node_conditions",
    "Get node status, conditions (Ready, MemoryPressure, DiskPressure, PIDPressure), "
    "and allocatable vs capacity resources. Pass node name.",
    {"node_name": str},
)
async def get_node_conditions(args: dict[str, Any]) -> dict[str, Any]:
    node_name = args.get("node_name", "")
    if not node_name:
        return _error("node_name is required.")

    try:
        node = await core_v1.read_node(name=node_name)
    except ApiException as e:
        return _api_error(e, f"Node {node_name}")

    status = node.status
    lines = [
        f"Node: {node_name}",
        f"  Age: {_age(node.metadata.creation_timestamp)}",
    ]

    # Node info
    info = status.node_info
    if info:
        lines.append(f"  OS: {info.os_image}")
        lines.append(f"  Kubelet: {info.kubelet_version}")
        lines.append(f"  Container Runtime: {info.container_runtime_version}")

    # Conditions
    lines.append("\n  Conditions:")
    for c in status.conditions or []:
        lines.append(f"    {c.type}: {c.status}" + (f" — {c.message}" if c.message else ""))

    # Capacity vs allocatable
    capacity = status.capacity or {}
    allocatable = status.allocatable or {}
    lines.append("\n  Resources:")
    lines.append(f"    {'Resource':<20} {'Capacity':<15} {'Allocatable':<15}")
    lines.append(f"    {'─' * 50}")
    for key in sorted(set(list(capacity.keys()) + list(allocatable.keys()))):
        cap = capacity.get(key, "—")
        alloc = allocatable.get(key, "—")
        lines.append(f"    {key:<20} {str(cap):<15} {str(alloc):<15}")

    # Taints
    taints = node.spec.taints or []
    if taints:
        lines.append(f"\n  Taints ({len(taints)}):")
        for t in taints:
            lines.append(f"    {t.key}={t.value or ''}:{t.effect}")

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 11: get_hpa_status
# ------------------------------------------------------------------
@tool(
    "get_hpa_status",
    "Get the status of a HorizontalPodAutoscaler — current/desired replicas, "
    "metric values, and conditions. Useful when diagnosing scaling issues.",
    {"namespace": str, "hpa_name": str},
)
async def get_hpa_status(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    hpa_name = args.get("hpa_name", "")

    if not hpa_name:
        return _error("hpa_name is required.")

    try:
        hpa = await autoscaling_v1.read_namespaced_horizontal_pod_autoscaler(
            name=hpa_name, namespace=ns
        )
    except ApiException as e:
        if e.status == 404:
            return _text(f"No HPA named '{hpa_name}' found in namespace '{ns}'.")
        return _api_error(e, f"HPA {hpa_name}")

    spec = hpa.spec
    status = hpa.status
    lines = [
        f"HPA: {ns}/{hpa_name}",
        f"  Target: {spec.scale_target_ref.kind}/{spec.scale_target_ref.name}",
        f"  Min Replicas: {spec.min_replicas}",
        f"  Max Replicas: {spec.max_replicas}",
        f"  Current Replicas: {status.current_replicas}",
        f"  Desired Replicas: {status.desired_replicas}",
    ]

    if spec.target_cpu_utilization_percentage:
        current_cpu = status.current_cpu_utilization_percentage
        lines.append(
            f"  CPU Target: {spec.target_cpu_utilization_percentage}%"
            + (f", Current: {current_cpu}%" if current_cpu is not None else "")
        )

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 12: get_pvc_status
# ------------------------------------------------------------------
@tool(
    "get_pvc_status",
    "Get the status of a PersistentVolumeClaim — phase (Bound/Pending), "
    "capacity, storage class, and access modes.",
    {"namespace": str, "pvc_name": str},
)
async def get_pvc_status(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    pvc_name = args.get("pvc_name", "")

    if not pvc_name:
        return _error("pvc_name is required.")

    try:
        pvc = await core_v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=ns)
    except ApiException as e:
        return _api_error(e, f"PVC {pvc_name}")

    spec = pvc.spec
    status = pvc.status
    lines = [
        f"PVC: {ns}/{pvc_name}",
        f"  Phase: {status.phase}",
        f"  StorageClass: {spec.storage_class_name or 'default'}",
        f"  Access Modes: {spec.access_modes}",
        f"  Requested: {spec.resources.requests.get('storage', '?') if spec.resources and spec.resources.requests else '?'}",
    ]

    if status.capacity:
        lines.append(f"  Capacity: {status.capacity.get('storage', '?')}")

    if pvc.spec.volume_name:
        lines.append(f"  Bound PV: {pvc.spec.volume_name}")
    else:
        lines.append("  Bound PV: (not bound)")

    # Check events for this PVC
    if status.phase == "Pending":
        lines.append("\n  ⚠ PVC is Pending — check events for provisioning errors.")

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 13: get_ingress_status
# ------------------------------------------------------------------
@tool(
    "get_ingress_status",
    "Get ingress rules, backend services, TLS configuration, and load balancer "
    "status for an Ingress resource.",
    {"namespace": str, "ingress_name": str},
)
async def get_ingress_status(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    ingress_name = args.get("ingress_name", "")

    if not ingress_name:
        return _error("ingress_name is required.")

    try:
        ingress = await networking_v1.read_namespaced_ingress(name=ingress_name, namespace=ns)
    except ApiException as e:
        return _api_error(e, f"Ingress {ingress_name}")

    spec = ingress.spec
    status = ingress.status
    lines = [
        f"Ingress: {ns}/{ingress_name}",
        f"  IngressClass: {spec.ingress_class_name or 'default'}",
    ]

    # TLS
    if spec.tls:
        lines.append("  TLS:")
        for tls in spec.tls:
            hosts = ", ".join(tls.hosts or ["*"])
            lines.append(f"    Hosts: {hosts}, Secret: {tls.secret_name or 'none'}")

    # Rules
    if spec.rules:
        lines.append("  Rules:")
        for rule in spec.rules:
            host = rule.host or "*"
            if rule.http and rule.http.paths:
                for path in rule.http.paths:
                    backend = path.backend
                    if backend.service:
                        svc = f"{backend.service.name}:{backend.service.port.number or backend.service.port.name}"
                    else:
                        svc = "unknown"
                    lines.append(f"    {host}{path.path or '/'} → {svc} ({path.path_type})")

    # Default backend
    if spec.default_backend:
        db = spec.default_backend
        if db.service:
            lines.append(f"  Default Backend: {db.service.name}:{db.service.port.number or db.service.port.name}")

    # Load balancer status
    if status and status.load_balancer and status.load_balancer.ingress:
        lines.append("  Load Balancer:")
        for lb in status.load_balancer.ingress:
            if lb.ip:
                lines.append(f"    IP: {lb.ip}")
            if lb.hostname:
                lines.append(f"    Hostname: {lb.hostname}")
    else:
        lines.append("  Load Balancer: (no address assigned)")

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 14: get_network_policy
# ------------------------------------------------------------------
@tool(
    "get_network_policy",
    "List NetworkPolicies in a namespace and show their pod selectors, ingress, "
    "and egress rules. Useful for diagnosing connectivity issues.",
    {"namespace": str, "pod_labels": str},
)
async def get_network_policy(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    pod_labels_str = args.get("pod_labels", "")

    # Parse pod labels from "key=value,key2=value2" format
    pod_labels: dict[str, str] = {}
    if pod_labels_str:
        for pair in pod_labels_str.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                pod_labels[k.strip()] = v.strip()

    try:
        result = await networking_v1.list_namespaced_network_policy(namespace=ns)
    except ApiException as e:
        return _api_error(e, f"NetworkPolicies in {ns}")

    policies = result.items
    if not policies:
        return _text(f"No NetworkPolicies found in namespace '{ns}'.")

    # Filter to policies that could affect the given pod labels
    matching = []
    for pol in policies:
        selector = pol.spec.pod_selector
        if not selector or not selector.match_labels:
            # Empty selector matches all pods
            matching.append(pol)
        elif pod_labels:
            # Check if pod_labels are a superset of the selector
            selector_labels = selector.match_labels or {}
            if all(pod_labels.get(k) == v for k, v in selector_labels.items()):
                matching.append(pol)
        else:
            matching.append(pol)

    lines = [f"NetworkPolicies in {ns}: {len(policies)} total, {len(matching)} affecting specified pod"]

    for pol in matching:
        lines.append(f"\n  Policy: {pol.metadata.name}")
        sel = pol.spec.pod_selector
        if sel and sel.match_labels:
            lines.append(f"    Pod Selector: {dict(sel.match_labels)}")
        else:
            lines.append("    Pod Selector: (all pods)")

        policy_types = pol.spec.policy_types or []
        lines.append(f"    Policy Types: {policy_types}")

        if pol.spec.ingress:
            lines.append("    Ingress Rules:")
            for rule in pol.spec.ingress:
                froms = rule._from or []
                ports = rule.ports or []
                port_str = ", ".join(f"{p.port}/{p.protocol}" for p in ports) or "all ports"
                if froms:
                    for f in froms:
                        if f.pod_selector:
                            lines.append(f"      From pods: {f.pod_selector.match_labels or 'all'} → {port_str}")
                        if f.namespace_selector:
                            lines.append(f"      From ns: {f.namespace_selector.match_labels or 'all'} → {port_str}")
                        if f.ip_block:
                            lines.append(f"      From CIDR: {f.ip_block.cidr} → {port_str}")
                else:
                    lines.append(f"      Allow all → {port_str}")

        if pol.spec.egress:
            lines.append("    Egress Rules:")
            for rule in pol.spec.egress:
                tos = rule.to or []
                ports = rule.ports or []
                port_str = ", ".join(f"{p.port}/{p.protocol}" for p in ports) or "all ports"
                if tos:
                    for t in tos:
                        if t.pod_selector:
                            lines.append(f"      To pods: {t.pod_selector.match_labels or 'all'} → {port_str}")
                        if t.namespace_selector:
                            lines.append(f"      To ns: {t.namespace_selector.match_labels or 'all'} → {port_str}")
                        if t.ip_block:
                            lines.append(f"      To CIDR: {t.ip_block.cidr} → {port_str}")
                else:
                    lines.append(f"      Allow all → {port_str}")

    return _text("\n".join(lines))
