"""The archetype catalog.

Each :class:`Archetype` describes a kind of challenge the LLM may select. The
LLM only picks an archetype id and supplies concrete ``params`` that satisfy the
archetype's ``params_schema``. Everything else (task text, manifests, broken
states, verifier assertions, reference solution) is rendered deterministically
by later phases.

Phase 2 ships the metadata + schemas for the core archetypes; renderers land in
Phase 3 and the full catalog in Phase 5.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..schemas import image_schema, label_schema, name_schema

# Official CKA 2025/2026 domain weightings (Linux Foundation).
DOMAINS: dict[str, int] = {
    "Storage": 10,
    "Troubleshooting": 30,
    "Workloads & Scheduling": 15,
    "Cluster Architecture, Installation & Configuration": 25,
    "Services & Networking": 20,
}


@dataclass(frozen=True)
class Archetype:
    id: str
    domain: str
    competency: str
    title: str
    description: str
    topics: tuple[str, ...]
    params_schema: dict
    extra_checks: Callable[[dict], list[str]] | None = None

    @property
    def weight(self) -> int:
        return DOMAINS[self.domain]


REGISTRY: dict[str, Archetype] = {}


def _register(arch: Archetype) -> Archetype:
    REGISTRY[arch.id] = arch
    return arch


_register(Archetype(
    id="deployment",
    domain="Workloads & Scheduling",
    competency="Understand application deployments and how to perform rolling update and rollbacks",
    title="Create a Deployment",
    description="Create a Deployment with a given image, replica count, and labels.",
    topics=("deployment", "workloads", "rollout", "replicas"),
    params_schema={
        "type": "object",
        "required": ["name", "namespace", "image", "replicas", "labels"],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "image": image_schema(),
            "replicas": {"type": "integer", "minimum": 1, "maximum": 50},
            "labels": label_schema(),
            "container_port": {"type": "integer", "minimum": 1, "maximum": 65535},
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="service",
    domain="Services & Networking",
    competency="Use ClusterIP, NodePort, LoadBalancer service types and endpoints",
    title="Expose a workload",
    description=(
        "A backend Deployment already exists; create a Service of the given type fronting it via labels."
    ),
    topics=("service", "networking", "nodeport", "loadbalancer", "clusterip"),
    params_schema={
        "type": "object",
        "required": ["name", "namespace", "service_type", "port", "target_port", "backend_labels"],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "service_type": {"type": "string", "enum": ["ClusterIP", "NodePort", "LoadBalancer"]},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "target_port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "backend_labels": label_schema(),
            "backend_image": image_schema(),
            "backend_replicas": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="networkpolicy",
    domain="Services & Networking",
    competency="Define and enforce Network Policies",
    title="Enforce a Network Policy",
    description=(
        "A target pod and a peer pod exist; create a NetworkPolicy allowing only the peer "
        "(by label) to reach the target on a given port."
    ),
    topics=("networkpolicy", "networking", "podsecurity"),
    params_schema={
        "type": "object",
        "required": ["name", "namespace", "target_labels", "peer_labels", "port"],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "target_labels": label_schema(),
            "peer_labels": label_schema(),
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "protocol": {"type": "string", "enum": ["TCP", "UDP"], "default": "TCP"},
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="rbac",
    domain="Cluster Architecture, Installation & Configuration",
    competency="Manage role based access control (RBAC)",
    title="Grant scoped access via RBAC",
    description=(
        "Create a ServiceAccount and bind a Role/ClusterRole that grants only the listed verbs "
        "on the listed resources."
    ),
    topics=("rbac", "role", "serviceaccount", "permissions"),
    params_schema={
        "type": "object",
        "required": ["sa_name", "namespace", "role_name", "role_kind", "resources", "verbs"],
        "properties": {
            "sa_name": name_schema(),
            "namespace": name_schema(),
            "role_name": name_schema(),
            "role_kind": {"type": "string", "enum": ["Role", "ClusterRole"]},
            "resources": {
                "type": "array", "minItems": 1, "maxItems": 10,
                "items": {"type": "string"},
            },
            "verbs": {
                "type": "array", "minItems": 1, "maxItems": 10,
                "items": {"type": "string"},
            },
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="pvc",
    domain="Storage",
    competency="Manage persistent volumes and persistent volume claims",
    title="Provision persistent storage",
    description=(
        "A StorageClass is available; create a PVC with the given access mode and size that binds."
    ),
    topics=("storage", "pvc", "persistentvolume", "storageclass"),
    params_schema={
        "type": "object",
        "required": ["name", "namespace", "access_mode", "size", "storage_class"],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "access_mode": {"type": "string", "enum": ["ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany"]},
            "size": {"type": "string", "pattern": r"^\d+(Mi|Gi)$"},
            "storage_class": {
                "oneOf": [name_schema(), {"type": "string", "const": ""}],
            },
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="scheduling",
    domain="Workloads & Scheduling",
    competency="Configure Pod admission and scheduling (limits, node affinity, etc.)",
    title="Schedule a workload on a labeled node",
    description=(
        "The node carries a label; run a Deployment there with a nodeSelector and resource "
        "requests/limits. (Taint/toleration variants need a multi-node environment and are "
        "on the roadmap.)"
    ),
    topics=("scheduling", "nodeselector", "resources", "limits", "affinity"),
    params_schema={
        "type": "object",
        "required": [
            "name", "namespace", "image", "replicas", "labels",
            "node_label_key", "node_label_value", "cpu_request", "memory_request",
        ],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "image": image_schema(),
            "replicas": {"type": "integer", "minimum": 1, "maximum": 10},
            "labels": label_schema(),
            "node_label_key": {"type": "string", "pattern": r"^[a-zA-Z0-9]([-_.a-zA-Z0-9]*[a-zA-Z0-9])?$"},
            "node_label_value": {"type": "string", "minLength": 1, "maxLength": 63},
            "cpu_request": {"type": "string", "pattern": r"^\d+(m)?$"},
            "memory_request": {"type": "string", "pattern": r"^\d+(Mi|Gi)$"},
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="troubleshooting_crashloop",
    domain="Troubleshooting",
    competency="Troubleshoot cluster components",
    title="Diagnose and fix a failing workload",
    description=(
        "A Deployment is crash-looping; identify the cause and fix it so all pods become Ready."
    ),
    topics=("troubleshooting", "crashloop", "diagnose", "logs", "probes"),
    params_schema={
        "type": "object",
        "required": ["name", "namespace", "image", "labels", "replicas", "failure"],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "image": image_schema(),
            "labels": label_schema(),
            "replicas": {"type": "integer", "minimum": 1, "maximum": 5},
            "failure": {"type": "string", "enum": ["bad_liveness", "exit_immediately"]},
        },
        "additionalProperties": False,
    },
))


def _simple_key_schema() -> dict:
    # No dots: these keys are queried via kubectl jsonpath `{.data.<key>}`.
    return {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$", "minLength": 1}


def _env_map_schema() -> dict:
    # Keys feed `envFrom` on the reference deployment, so they must be valid
    # environment-variable names or the pod fails to start.
    return {
        "type": "object",
        "maxProperties": 10,
        "propertyNames": {"pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"},
        "additionalProperties": {"type": "string"},
    }


_register(Archetype(
    id="configmap_secret",
    domain="Workloads & Scheduling",
    competency="Use ConfigMaps and Secrets to configure applications",
    title="Configure an app with ConfigMaps and Secrets",
    description=(
        "Create a ConfigMap and a Secret with the given data, plus a Deployment that "
        "consumes both via envFrom."
    ),
    topics=("configmap", "secret", "env", "configuration"),
    params_schema={
        "type": "object",
        "required": ["name", "namespace", "cm_data", "secret_data", "deploy_name", "image", "replicas", "labels"],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "cm_data": _env_map_schema(),
            "secret_data": _env_map_schema(),
            "deploy_name": name_schema(),
            "image": image_schema(),
            "replicas": {"type": "integer", "minimum": 1, "maximum": 10},
            "labels": label_schema(),
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="fix_served_file",
    domain="Troubleshooting",
    competency="Troubleshoot cluster components",
    title="Fix the content served by a web app",
    description=(
        "A Deployment serves a web page from a ConfigMap; the page content is wrong. "
        "Update the ConfigMap so the served page contains the expected text."
    ),
    topics=("troubleshooting", "configmap", "web", "files"),
    params_schema={
        "type": "object",
        "required": ["name", "namespace", "labels", "expected_content", "bad_content"],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "labels": label_schema(),
            "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 80},
            "expected_content": {"type": "string", "minLength": 1, "maxLength": 500},
            "bad_content": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="cni_config",
    domain="Cluster Architecture, Installation & Configuration",
    competency="Understand extension interfaces (CNI, CSI, CRI, etc.)",
    title="Complete a CNI config file",
    description=(
        "An incomplete CNI-style config file is provided in the exam workdir. Complete it "
        "and create a ConfigMap from it in the cluster."
    ),
    topics=("cni", "networking", "extension", "files"),
    params_schema={
        "type": "object",
        "required": ["name", "namespace", "plugin_type", "cni_version"],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "key": _simple_key_schema(),
            "plugin_type": {"type": "string", "minLength": 1},
            "cni_version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="autoscaling",
    domain="Workloads & Scheduling",
    competency="Configure workload autoscaling",
    title="Configure a HorizontalPodAutoscaler",
    description=(
        "A Deployment already exists; create a HorizontalPodAutoscaler targeting it with "
        "given min/max replicas and a CPU utilization target."
    ),
    topics=("autoscaling", "hpa", "scaling", "metrics"),
    params_schema={
        "type": "object",
        "required": ["name", "namespace", "workload", "min", "max", "cpu_target", "image", "replicas", "labels"],
        "properties": {
            "name": name_schema(),
            "namespace": name_schema(),
            "workload": name_schema(),
            "min": {"type": "integer", "minimum": 1, "maximum": 20},
            "max": {"type": "integer", "minimum": 1, "maximum": 50},
            "cpu_target": {"type": "integer", "minimum": 1, "maximum": 100},
            "image": image_schema(),
            "replicas": {"type": "integer", "minimum": 1, "maximum": 10},
            "labels": label_schema(),
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="helm",
    domain="Cluster Architecture, Installation & Configuration",
    competency="Use Helm and Kustomize to install cluster components",
    title="Install a chart with Helm",
    description=(
        "A Helm chart is provided in the exam workdir. Install it with the given release "
        "name in the given namespace, overriding image and replicas."
    ),
    topics=("helm", "charts", "install"),
    params_schema={
        "type": "object",
        "required": ["release", "namespace", "image", "replicas"],
        "properties": {
            "release": name_schema(),
            "namespace": name_schema(),
            "chart_name": name_schema(),
            "image": image_schema(),
            "replicas": {"type": "integer", "minimum": 1, "maximum": 10},
            "service_port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 80},
        },
        "additionalProperties": False,
    },
))

_register(Archetype(
    id="kustomize",
    domain="Cluster Architecture, Installation & Configuration",
    competency="Use Helm and Kustomize to install cluster components",
    title="Deploy an overlay with Kustomize",
    description=(
        "A kustomize base is provided in the exam workdir. Create an overlay that sets the "
        "namespace, prefixes names, and patches the image; apply it with kubectl -k."
    ),
    topics=("kustomize", "overlay", "patches"),
    params_schema={
        "type": "object",
        "required": ["namespace", "image", "replicas"],
        "properties": {
            "namespace": name_schema(),
            "overlay": name_schema(),
            "name_prefix": {"type": "string", "pattern": r"^[a-zA-Z0-9-]+$", "minLength": 1},
            "base_name": name_schema(),
            "image": image_schema(),
            "replicas": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "additionalProperties": False,
    },
))


def summarize_schema(schema: dict) -> str:
    """Compact human-readable description of a params schema for the prompt."""
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    parts = []
    for name, spec in props.items():
        kind = spec.get("type", "any")
        if kind == "string":
            hint = ""
            if "enum" in spec:
                hint = " | ".join(str(x) for x in spec["enum"])
            elif "pattern" in spec:
                hint = "regex:" + spec["pattern"]
            if hint:
                kind = f"string({hint})"
        elif kind == "integer":
            kind = f"integer({spec.get('minimum', '')}..{spec.get('maximum', '')})"
        elif kind == "array":
            kind = f"array of {spec.get('items', {}).get('type', 'any')}"
        elif kind == "object":
            kind = "object of label:value"
        req = "required" if name in required else "optional"
        parts.append(f"{name}:{kind}[{req}]")
    return "; ".join(parts)
