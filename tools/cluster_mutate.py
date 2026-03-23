"""Mutation tools for Kubernetes resources.

THESE TOOLS MODIFY CLUSTER STATE — they require human approval before execution.
Every mutation:
  1. Supports dry_run="true" mode (server-side dry run via K8s API)
  2. Returns the diff/preview so the human can verify before approving
  3. Captures pre-state snapshot for rollback

Available mutations (deliberately limited scope):
  - patch_resource:       JSON strategic-merge patch on any supported resource
  - scale_deployment:     Set replica count
  - rollback_deployment:  Undo to previous revision
  - restart_deployment:   Rolling restart (annotation bump)
  - delete_pod:           Delete a pod to force reschedule
  - create_resource:      Create a resource from a YAML/dict spec
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from kubernetes.client import ApiClient
from kubernetes.client.rest import ApiException

from ..k8s_client import apps_v1, core_v1
from .decorator import tool

logger = logging.getLogger(__name__)


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
    if e.status == 409:
        return _error(f"Conflict applying change to {resource}. Resource may have been modified.")
    if e.status == 422:
        return _error(f"Invalid change for {resource}: {e.reason}")
    return _error(f"Kubernetes API error ({e.status}): {e.reason}")


def _serialize(obj: Any) -> dict:
    """Convert a K8s API object to a serializable dict."""
    return ApiClient().sanitize_for_serialization(obj)


# ------------------------------------------------------------------
# Tool 1: patch_resource
# ------------------------------------------------------------------
@tool(
    "patch_resource",
    "Apply a JSON strategic-merge patch to a Kubernetes resource. "
    "Supports: deployment, statefulset, daemonset, service, configmap. "
    "Set dry_run=true to preview the change without applying it. "
    "The patch should be a JSON object with the fields to merge.",
    {
        "kind": str,
        "namespace": str,
        "name": str,
        "patch_json": str,
        "dry_run": str,
    },
)
async def patch_resource(args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind", "").lower()
    ns = args.get("namespace", "default")
    name = args.get("name", "")
    patch_json_str = args.get("patch_json", "")
    dry_run = args.get("dry_run", "true").lower() == "true"

    if not name:
        return _error("name is required.")
    if not patch_json_str:
        return _error("patch_json is required.")

    try:
        patch_body = json.loads(patch_json_str)
    except json.JSONDecodeError as e:
        return _error(f"Invalid JSON in patch_json: {e}")

    dry_run_param = ["All"] if dry_run else None
    mode = "DRY RUN" if dry_run else "LIVE"

    try:
        if kind == "deployment":
            # Capture pre-state
            pre = _serialize(await apps_v1.read_namespaced_deployment(name=name, namespace=ns))
            result = await apps_v1.patch_namespaced_deployment(
                name=name, namespace=ns, body=patch_body, dry_run=dry_run_param,
            )
        elif kind in ("statefulset", "sts"):
            pre = _serialize(await apps_v1.read_namespaced_stateful_set(name=name, namespace=ns))
            result = await apps_v1.patch_namespaced_stateful_set(
                name=name, namespace=ns, body=patch_body, dry_run=dry_run_param,
            )
        elif kind in ("daemonset", "ds"):
            pre = _serialize(await apps_v1.read_namespaced_daemon_set(name=name, namespace=ns))
            result = await apps_v1.patch_namespaced_daemon_set(
                name=name, namespace=ns, body=patch_body, dry_run=dry_run_param,
            )
        elif kind in ("service", "svc"):
            pre = _serialize(await core_v1.read_namespaced_service(name=name, namespace=ns))
            result = await core_v1.patch_namespaced_service(
                name=name, namespace=ns, body=patch_body, dry_run=dry_run_param,
            )
        elif kind in ("configmap", "cm"):
            pre = _serialize(await core_v1.read_namespaced_config_map(name=name, namespace=ns))
            result = await core_v1.patch_namespaced_config_map(
                name=name, namespace=ns, body=patch_body, dry_run=dry_run_param,
            )
        else:
            return _error(
                f"Unsupported kind '{kind}'. "
                "Supported: deployment, statefulset, daemonset, service, configmap."
            )
    except ApiException as e:
        return _api_error(e, f"{kind}/{name}")

    post = _serialize(result)

    lines = [
        f"[{mode}] Patched {kind}/{name} in {ns}",
        f"Patch applied: {json.dumps(patch_body, indent=2)}",
    ]

    # Show key diffs
    diff = _compute_simple_diff(pre, post, kind)
    if diff:
        lines.append(f"\nChanges detected:")
        lines.extend(f"  {d}" for d in diff)
    else:
        lines.append("\nNo effective changes detected.")

    if dry_run:
        lines.append("\n⚠️ This was a DRY RUN — no changes were applied to the cluster.")

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 2: scale_deployment
# ------------------------------------------------------------------
@tool(
    "scale_deployment",
    "Scale a Deployment, StatefulSet, or ReplicaSet to a specific replica count. "
    "Set dry_run=true to preview. Returns before/after replica counts.",
    {
        "kind": str,
        "namespace": str,
        "name": str,
        "replicas": str,
        "dry_run": str,
    },
)
async def scale_deployment(args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind", "deployment").lower()
    ns = args.get("namespace", "default")
    name = args.get("name", "")
    replicas_str = args.get("replicas", "")
    dry_run = args.get("dry_run", "true").lower() == "true"

    if not name:
        return _error("name is required.")
    if not replicas_str:
        return _error("replicas is required.")

    try:
        replicas = int(replicas_str)
    except ValueError:
        return _error(f"replicas must be an integer, got '{replicas_str}'.")

    if replicas < 0:
        return _error("replicas cannot be negative.")
    if replicas > 100:
        return _error("Safety limit: replicas cannot exceed 100. Override manually if needed.")

    dry_run_param = ["All"] if dry_run else None
    mode = "DRY RUN" if dry_run else "LIVE"

    scale_body = {"spec": {"replicas": replicas}}

    try:
        if kind == "deployment":
            current = await apps_v1.read_namespaced_deployment(name=name, namespace=ns)
            old_replicas = current.spec.replicas
            await apps_v1.patch_namespaced_deployment_scale(
                name=name, namespace=ns, body=scale_body, dry_run=dry_run_param,
            )
        elif kind in ("statefulset", "sts"):
            current = await apps_v1.read_namespaced_stateful_set(name=name, namespace=ns)
            old_replicas = current.spec.replicas
            await apps_v1.patch_namespaced_stateful_set_scale(
                name=name, namespace=ns, body=scale_body, dry_run=dry_run_param,
            )
        else:
            return _error(f"Unsupported kind '{kind}'. Supported: deployment, statefulset.")
    except ApiException as e:
        return _api_error(e, f"{kind}/{name}")

    lines = [
        f"[{mode}] Scaled {kind}/{name} in {ns}",
        f"  Replicas: {old_replicas} → {replicas}",
    ]
    if dry_run:
        lines.append("\n⚠️ This was a DRY RUN — no changes were applied.")

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 3: rollback_deployment
# ------------------------------------------------------------------
@tool(
    "rollback_deployment",
    "Rollback a Deployment to its previous revision. This uses the K8s rollout "
    "undo mechanism. Set dry_run=true to preview what revision it would roll back to.",
    {
        "namespace": str,
        "name": str,
        "revision": str,
        "dry_run": str,
    },
)
async def rollback_deployment(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    name = args.get("name", "")
    revision_str = args.get("revision", "0")  # 0 = previous
    dry_run = args.get("dry_run", "true").lower() == "true"

    if not name:
        return _error("name is required.")

    try:
        revision = int(revision_str)
    except ValueError:
        return _error(f"revision must be an integer, got '{revision_str}'.")

    mode = "DRY RUN" if dry_run else "LIVE"

    try:
        # Get current deployment state
        current = await apps_v1.read_namespaced_deployment(name=name, namespace=ns)
        current_image = _get_container_images(current)
        current_revision = current.metadata.annotations.get(
            "deployment.kubernetes.io/revision", "unknown"
        )

        # List ReplicaSets to find the target revision
        rs_list = await apps_v1.list_namespaced_replica_set(
            namespace=ns,
            label_selector=",".join(
                f"{k}={v}" for k, v in (current.spec.selector.match_labels or {}).items()
            ),
        )

        # Sort by revision annotation
        revision_map = {}
        for rs in rs_list.items:
            rev = rs.metadata.annotations.get("deployment.kubernetes.io/revision", "0")
            revision_map[int(rev)] = rs

        if revision == 0:
            # Previous = current revision - 1
            target_rev = int(current_revision) - 1 if current_revision != "unknown" else 0
        else:
            target_rev = revision

        target_rs = revision_map.get(target_rev)
        if not target_rs:
            available = sorted(revision_map.keys())
            return _error(
                f"Revision {target_rev} not found. Available revisions: {available}"
            )

        target_image = _get_rs_container_images(target_rs)

        if dry_run:
            lines = [
                f"[DRY RUN] Rollback preview for deployment/{name} in {ns}",
                f"  Current revision: {current_revision}",
                f"  Target revision: {target_rev}",
                f"  Current images: {current_image}",
                f"  Target images: {target_image}",
                "\n⚠️ This was a DRY RUN — no changes were applied.",
            ]
            return _text("\n".join(lines))

        # Execute rollback by patching the deployment with the target RS's template
        rollback_patch = {
            "spec": {
                "template": _serialize(target_rs.spec.template),
            }
        }
        await apps_v1.patch_namespaced_deployment(
            name=name, namespace=ns, body=rollback_patch,
        )

        lines = [
            f"[LIVE] Rolled back deployment/{name} in {ns}",
            f"  From revision: {current_revision} → {target_rev}",
            f"  Images: {current_image} → {target_image}",
            "  Rollout in progress — monitor with get_pod_status.",
        ]
        return _text("\n".join(lines))

    except ApiException as e:
        return _api_error(e, f"deployment/{name}")


# ------------------------------------------------------------------
# Tool 4: restart_deployment
# ------------------------------------------------------------------
@tool(
    "restart_deployment",
    "Trigger a rolling restart of a Deployment by bumping the restart annotation. "
    "Equivalent to 'kubectl rollout restart'. Pods are replaced one by one.",
    {
        "namespace": str,
        "name": str,
        "dry_run": str,
    },
)
async def restart_deployment(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    name = args.get("name", "")
    dry_run = args.get("dry_run", "true").lower() == "true"

    if not name:
        return _error("name is required.")

    dry_run_param = ["All"] if dry_run else None
    mode = "DRY RUN" if dry_run else "LIVE"

    now = datetime.now(timezone.utc).isoformat()
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now,
                    }
                }
            }
        }
    }

    try:
        current = await apps_v1.read_namespaced_deployment(name=name, namespace=ns)
        replicas = current.spec.replicas or 1
        strategy = current.spec.strategy.type if current.spec.strategy else "RollingUpdate"

        await apps_v1.patch_namespaced_deployment(
            name=name, namespace=ns, body=patch_body, dry_run=dry_run_param,
        )
    except ApiException as e:
        return _api_error(e, f"deployment/{name}")

    lines = [
        f"[{mode}] Rolling restart of deployment/{name} in {ns}",
        f"  Replicas: {replicas}",
        f"  Strategy: {strategy}",
        f"  Restart annotation set to: {now}",
    ]
    if dry_run:
        lines.append("\n⚠️ This was a DRY RUN — no pods will be restarted.")
    else:
        lines.append("\nPods will be replaced according to the deployment's update strategy.")

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 5: delete_pod
# ------------------------------------------------------------------
@tool(
    "delete_pod",
    "Delete a specific pod to force Kubernetes to reschedule it. "
    "The owning controller (Deployment, StatefulSet, etc.) will create a replacement. "
    "Use for: stuck pods, pods on bad nodes, forcing config reload.",
    {
        "namespace": str,
        "pod_name": str,
        "grace_period_seconds": str,
        "dry_run": str,
    },
)
async def delete_pod(args: dict[str, Any]) -> dict[str, Any]:
    ns = args.get("namespace", "default")
    pod_name = args.get("pod_name", "")
    grace_str = args.get("grace_period_seconds", "30")
    dry_run = args.get("dry_run", "true").lower() == "true"

    if not pod_name:
        return _error("pod_name is required.")

    try:
        grace_period = int(grace_str)
    except ValueError:
        grace_period = 30

    dry_run_param = ["All"] if dry_run else None
    mode = "DRY RUN" if dry_run else "LIVE"

    try:
        # Get pod info before deleting
        pod = await core_v1.read_namespaced_pod(name=pod_name, namespace=ns)
        node = pod.spec.node_name or "unscheduled"
        owner = "none"
        if pod.metadata.owner_references:
            ref = pod.metadata.owner_references[0]
            owner = f"{ref.kind}/{ref.name}"

        await core_v1.delete_namespaced_pod(
            name=pod_name,
            namespace=ns,
            grace_period_seconds=grace_period,
            dry_run=dry_run_param,
        )
    except ApiException as e:
        return _api_error(e, f"pod/{pod_name}")

    lines = [
        f"[{mode}] Deleted pod {ns}/{pod_name}",
        f"  Node: {node}",
        f"  Owner: {owner}",
        f"  Grace period: {grace_period}s",
    ]

    if owner == "none":
        lines.append("\n⚠️ WARNING: This pod has no owner controller — it will NOT be recreated!")
    else:
        lines.append(f"\nThe {owner} will create a replacement pod.")

    if dry_run:
        lines.append("\n⚠️ This was a DRY RUN — the pod was NOT deleted.")

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Tool 6: create_resource
# ------------------------------------------------------------------
@tool(
    "create_resource",
    "Create a Kubernetes resource from a JSON spec. "
    "Supports: configmap, secret (Opaque type only), service. "
    "Set dry_run=true to validate without creating.",
    {
        "kind": str,
        "namespace": str,
        "resource_json": str,
        "dry_run": str,
    },
)
async def create_resource(args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind", "").lower()
    ns = args.get("namespace", "default")
    resource_json_str = args.get("resource_json", "")
    dry_run = args.get("dry_run", "true").lower() == "true"

    if not resource_json_str:
        return _error("resource_json is required.")

    try:
        body = json.loads(resource_json_str)
    except json.JSONDecodeError as e:
        return _error(f"Invalid JSON: {e}")

    dry_run_param = ["All"] if dry_run else None
    mode = "DRY RUN" if dry_run else "LIVE"

    try:
        if kind in ("configmap", "cm"):
            result = await core_v1.create_namespaced_config_map(
                namespace=ns, body=body, dry_run=dry_run_param,
            )
            name = result.metadata.name
        elif kind == "secret":
            # Safety: only allow Opaque secrets
            if body.get("type", "Opaque") != "Opaque":
                return _error("Only Opaque secrets can be created by the agent.")
            result = await core_v1.create_namespaced_secret(
                namespace=ns, body=body, dry_run=dry_run_param,
            )
            name = result.metadata.name
        elif kind in ("service", "svc"):
            result = await core_v1.create_namespaced_service(
                namespace=ns, body=body, dry_run=dry_run_param,
            )
            name = result.metadata.name
        else:
            return _error(
                f"Unsupported kind '{kind}'. Supported: configmap, secret, service."
            )
    except ApiException as e:
        if e.status == 409:
            return _error(f"{kind} already exists in {ns}. Use patch_resource to update it.")
        return _api_error(e, f"{kind} in {ns}")

    lines = [
        f"[{mode}] Created {kind}/{name} in {ns}",
    ]
    if dry_run:
        lines.append("⚠️ This was a DRY RUN — the resource was NOT created.")
    else:
        lines.append("Resource created successfully.")

    return _text("\n".join(lines))


# ------------------------------------------------------------------
# Diff helpers
# ------------------------------------------------------------------
def _compute_simple_diff(pre: dict, post: dict, kind: str) -> list[str]:
    """Compute a human-readable diff of key fields between pre and post states."""
    diffs: list[str] = []

    if kind in ("deployment", "statefulset", "daemonset"):
        # Compare container specs
        pre_containers = _extract_containers(pre)
        post_containers = _extract_containers(post)
        for cname in set(list(pre_containers.keys()) + list(post_containers.keys())):
            pre_c = pre_containers.get(cname, {})
            post_c = post_containers.get(cname, {})
            for field in ("image", "resources"):
                pre_val = pre_c.get(field)
                post_val = post_c.get(field)
                if pre_val != post_val:
                    diffs.append(f"container/{cname}.{field}: {pre_val} → {post_val}")

        # Compare replicas
        pre_replicas = pre.get("spec", {}).get("replicas")
        post_replicas = post.get("spec", {}).get("replicas")
        if pre_replicas != post_replicas:
            diffs.append(f"spec.replicas: {pre_replicas} → {post_replicas}")

    elif kind in ("configmap", "cm"):
        pre_data = set((pre.get("data") or {}).keys())
        post_data = set((post.get("data") or {}).keys())
        added = post_data - pre_data
        removed = pre_data - post_data
        if added:
            diffs.append(f"data keys added: {added}")
        if removed:
            diffs.append(f"data keys removed: {removed}")

    return diffs


def _extract_containers(obj: dict) -> dict[str, dict]:
    """Extract container specs keyed by name."""
    containers = (
        obj.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    )
    return {c.get("name", "unnamed"): c for c in containers}


def _get_container_images(deployment) -> str:
    """Get comma-separated image list from a deployment."""
    containers = deployment.spec.template.spec.containers or []
    return ", ".join(c.image for c in containers)


def _get_rs_container_images(rs) -> str:
    """Get comma-separated image list from a ReplicaSet."""
    containers = rs.spec.template.spec.containers or []
    return ", ".join(c.image for c in containers)
