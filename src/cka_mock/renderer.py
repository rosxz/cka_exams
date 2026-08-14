"""Deterministic renderer: turns a validated question into everything the tool
needs — task text, setup manifests, verifier assertions, and a reference
solution. All content here comes from archetype templates + params; the LLM
never authorizes file or manifest bytes.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import yaml

from .archetypes import REGISTRY
from .assertion import (
    Assertion,
    CountAssertion,
    ExecAssertion,
    ExecContentAssertion,
    ResourceAssertion,
)
from .manifests import fetch_manifest, rewrite_contour_manifest
from .schemas import QuestionSpec

NODE_TOKEN = "{{NODE}}"
FILES_TOKEN = "{{FILES}}"


@dataclass
class ExamFile:
    path: str
    content: str
    mount_via: str = "workdir"  # "workdir" | "configmap" | "node"


@dataclass
class RenderResult:
    archetype_id: str
    question_index: int
    task: str
    setup_manifests: list[dict] = field(default_factory=list)
    setup_commands: list[list[str]] = field(default_factory=list)
    assertions: list[Assertion] = field(default_factory=list)
    reference_manifests: list[dict] = field(default_factory=list)
    reference_commands: list[list[str]] = field(default_factory=list)
    files: list[ExamFile] = field(default_factory=list)
    node_required: bool = False
    # Install-yourself challenges (Gateway API / operators): commands that
    # install cluster-scoped components (CRDs, controller) before the reference
    # resources can be created, assertions that must pass once the controller is
    # up, and commands to fully tear the install down so the candidate starts
    # from a clean cluster.
    reference_install_commands: list[list[str]] = field(default_factory=list)
    reference_ready_assertions: list[Assertion] = field(default_factory=list)
    reference_teardown_commands: list[list[str]] = field(default_factory=list)


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _namespace_doc(namespace: str) -> dict:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}


def _deployment_doc(
    name: str, namespace: str, image: str, replicas: int, labels: dict,
    *,
    container_port: int | None = None,
    command: list[str] | None = None,
    liveness_path: str | None = None,
    tolerations: list[dict] | None = None,
    volume_mounts: list[dict] | None = None,
    volumes: list[dict] | None = None,
    resources: dict | None = None,
) -> dict:
    container: dict = {"name": "app", "image": image}
    if container_port:
        container["ports"] = [{"containerPort": container_port}]
    if command:
        container["command"] = command
    if liveness_path:
        container["livenessProbe"] = {
            "httpGet": {"path": liveness_path, "port": container_port or 80},
            "periodSeconds": 5,
        }
    if volume_mounts:
        container["volumeMounts"] = volume_mounts
    if resources:
        container["resources"] = resources
    pod_spec: dict = {"containers": [container]}
    if tolerations:
        pod_spec["tolerations"] = tolerations
    if volumes:
        pod_spec["volumes"] = volumes
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": pod_spec,
            },
        },
    }


def _service_doc(name, namespace, service_type, port, target_port, selector) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "type": service_type,
            "selector": selector,
            "ports": [{"port": port, "targetPort": target_port}],
        },
    }


def _pod_doc(name, namespace, image, labels, command) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {"containers": [{"name": "app", "image": image, "command": command}]},
    }


def _selector_string(labels: dict) -> str:
    return ",".join(f"{key}={value}" for key, value in labels.items())


def _labels_repr(labels: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in labels.items())


# --- archetype renderers -----------------------------------------------------


def render_deployment(q: QuestionSpec) -> RenderResult:
    p = q.params
    labels = p["labels"]
    task = (
        f"Create a Deployment named `{p['name']}` in namespace `{p['namespace']}` "
        f"running image `{p['image']}` with {p['replicas']} replica(s). The pods must carry the "
        f"labels {_labels_repr(labels)}."
    )
    if p.get("container_port"):
        task += f" The container should expose port {p['container_port']}."
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(p["namespace"])],
    )
    dep = _deployment_doc(
        p["name"], p["namespace"], p["image"], p["replicas"], labels,
        container_port=p.get("container_port"),
    )
    r.assertions = [
        ResourceAssertion("deployments.apps", p["name"], p["namespace"]),
        ResourceAssertion("deployments.apps", p["name"], p["namespace"], "{.spec.replicas}", p["replicas"]),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.spec.template.spec.containers[0].image}", p["image"],
        ),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.spec.template.metadata.labels}", labels, "superset",
        ),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.status.availableReplicas}", p["replicas"], "gte",
        ),
    ]
    r.reference_manifests = [dep]
    return r


def render_service(q: QuestionSpec) -> RenderResult:
    p = q.params
    backend = f"{p['name']}-backend"
    labels = p["backend_labels"]
    target_port = p["target_port"]
    task = (
        f"In namespace `{p['namespace']}`, a Deployment named `{backend}` with labels "
        f"{_labels_repr(labels)} already exists. Create a Service named `{p['name']}` of type "
        f"`{p['service_type']}` exposing port {p['port']} to targetPort {target_port}."
    )
    backend_image = p.get("backend_image", "nginx:1.27")
    backend_replicas = p.get("backend_replicas", 1)
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(p["namespace"]),
            _deployment_doc(
                backend, p["namespace"], backend_image, backend_replicas, labels,
                container_port=target_port,
            ),
        ],
    )
    r.assertions = [
        ResourceAssertion("services", p["name"], p["namespace"]),
        ResourceAssertion("services", p["name"], p["namespace"], "{.spec.type}", p["service_type"]),
        ResourceAssertion("services", p["name"], p["namespace"], "{.spec.ports[0].port}", p["port"]),
        ResourceAssertion("services", p["name"], p["namespace"], "{.spec.ports[0].targetPort}", target_port),
        ResourceAssertion("services", p["name"], p["namespace"], "{.spec.selector}", labels, "eq"),
        ResourceAssertion("endpoints", p["name"], p["namespace"], "{.subsets[0].addresses[*].ip}", op="nonempty"),
    ]
    r.reference_manifests = [_service_doc(p["name"], p["namespace"], p["service_type"], p["port"], target_port, labels)]
    return r


def render_networkpolicy(q: QuestionSpec) -> RenderResult:
    p = q.params
    name = p["name"]
    namespace = p["namespace"]
    target = f"{name}-target"
    target_svc = f"{name}-svc"
    peer = f"{name}-peer"
    blocked = f"{name}-blocked"
    port = p["port"]
    protocol = p.get("protocol", "TCP")
    target_labels = p["target_labels"]
    peer_labels = p["peer_labels"]

    task = (
        f"In namespace `{namespace}`, a target Deployment (`{target}`, labels {_labels_repr(target_labels)}) "
        f"is exposed by Service `{target_svc}` on port {port}. Two probe Pods exist. Create a NetworkPolicy "
        f"named `{name}` allowing ingress to pods with labels {_labels_repr(target_labels)} ONLY from pods "
        f"with labels {_labels_repr(peer_labels)} on port {port}/{protocol}."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(namespace),
            _deployment_doc(
                target, namespace, "python:3.12-slim", 1, target_labels,
                container_port=port,
                command=["python3", "-m", "http.server", str(port)],
            ),
            _service_doc(target_svc, namespace, "ClusterIP", port, port, target_labels),
            _pod_doc(peer, namespace, "busybox:1.36", peer_labels, ["sh", "-c", "sleep 3600"]),
            _pod_doc(blocked, namespace, "busybox:1.36", {"app": "blocked"}, ["sh", "-c", "sleep 3600"]),
        ],
    )
    probe = ["wget", "-qO-", "-T", "2", f"http://{target_svc}:{port}/"]
    r.assertions = [
        ResourceAssertion("networkpolicies.networking.k8s.io", name, namespace),
        ExecAssertion(peer, namespace, probe, expect_rc=0),
        ExecAssertion(blocked, namespace, probe, expect_rc=0, op="ne"),
    ]
    r.reference_manifests = [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "podSelector": {"matchLabels": target_labels},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": peer_labels}}],
                        "ports": [{"protocol": protocol, "port": port}],
                    }
                ],
            },
        }
    ]
    return r


def render_rbac(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    sa_name = p["sa_name"]
    role_name = p["role_name"]
    role_kind = p["role_kind"]
    binding_name = f"{role_name}-binding"
    resources = p["resources"]
    verbs = p["verbs"]
    cluster_scoped = role_kind == "ClusterRole"

    task = (
        f"In namespace `{namespace}`, create a ServiceAccount `{sa_name}` and a `{role_kind}` named "
        f"`{role_name}` granting the verbs {verbs} on the resources {resources}. Then bind them with "
        f"a RoleBinding named `{binding_name}`."
    )
    role_resource = "clusterroles.rbac.authorization.k8s.io" if cluster_scoped else "roles.rbac.authorization.k8s.io"
    binding_resource = (
        "clusterrolebindings.rbac.authorization.k8s.io" if cluster_scoped
        else "rolebindings.rbac.authorization.k8s.io"
    )
    binding_namespace = None if cluster_scoped else namespace

    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace)],
    )
    r.assertions = [
        ResourceAssertion("serviceaccounts", sa_name, namespace),
        ResourceAssertion(role_resource, role_name, binding_namespace),
        ResourceAssertion(role_resource, role_name, binding_namespace, "{.rules[*].resources[*]}", resources, "superset"),
        ResourceAssertion(role_resource, role_name, binding_namespace, "{.rules[*].verbs[*]}", verbs, "superset"),
        ResourceAssertion(binding_resource, binding_name, binding_namespace),
        ResourceAssertion(binding_resource, binding_name, binding_namespace, "{.subjects[*].name}", sa_name, "contains"),
    ]
    role_metadata: dict = {"name": role_name}
    binding_metadata: dict = {"name": binding_name}
    if not cluster_scoped:
        role_metadata["namespace"] = namespace
        binding_metadata["namespace"] = namespace
    r.reference_manifests = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": sa_name, "namespace": namespace}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": role_kind,
            "metadata": role_metadata,
            "rules": [{"apiGroups": [""], "resources": resources, "verbs": verbs}],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding" if cluster_scoped else "RoleBinding",
            "metadata": binding_metadata,
            "subjects": [{"kind": "ServiceAccount", "name": sa_name, "namespace": namespace}],
            "roleRef": {"kind": role_kind, "name": role_name, "apiGroup": "rbac.authorization.k8s.io"},
        },
    ]
    return r


def render_pvc(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    name = p["name"]
    storage_class = p.get("storage_class")
    task = (
        f"In namespace `{namespace}`, create a PersistentVolumeClaim named `{name}` with access mode "
        f"`{p['access_mode']}` requesting `{p['size']}`"
    )
    setup_manifests = [_namespace_doc(namespace)]
    if storage_class and storage_class not in ("standard", "local-path"):
        setup_manifests.append(
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": storage_class},
                "provisioner": "k8s.io/minikube-hostpath",
                "reclaimPolicy": "Delete",
                "volumeBindingMode": "Immediate",
            }
        )
        task += f" using the storage class `{storage_class}`."
    elif storage_class == "standard":
        task += " using the `standard` storage class."
    else:
        task += " using the default storage class."

    r = RenderResult(archetype_id=q.archetype_id, question_index=0, task=task, setup_manifests=setup_manifests)
    pvc_manifest: dict = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "accessModes": [p["access_mode"]],
            "resources": {"requests": {"storage": p["size"]}},
        },
    }
    if storage_class and storage_class != "":
        pvc_manifest["spec"]["storageClassName"] = storage_class

    r.assertions = [
        ResourceAssertion("persistentvolumeclaims", name, namespace),
        ResourceAssertion("persistentvolumeclaims", name, namespace, "{.spec.accessModes[0]}", p["access_mode"]),
        ResourceAssertion("persistentvolumeclaims", name, namespace, "{.status.phase}", "Bound"),
    ]
    if storage_class and storage_class != "":
        r.assertions.append(
            ResourceAssertion("persistentvolumeclaims", name, namespace, "{.spec.storageClassName}", storage_class)
        )
    r.reference_manifests = [pvc_manifest]
    return r


def render_scheduling(q: QuestionSpec) -> RenderResult:
    p = q.params
    labels = p["labels"]
    node_selector = {p["node_label_key"]: p["node_label_value"]}
    task = (
        f"The cluster node carries the label `{p['node_label_key']}={p['node_label_value']}`. "
        f"Create a Deployment named `{p['name']}` in namespace `{p['namespace']}` running "
        f"`{p['image']}` with {p['replicas']} replica(s) and labels {_labels_repr(labels)}. The pods "
        f"must be scheduled on the labeled node (nodeSelector) and request cpu "
        f"`{p['cpu_request']}` and memory `{p['memory_request']}`."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(p["namespace"])],
        setup_commands=[["label", "nodes", NODE_TOKEN, f"{p['node_label_key']}={p['node_label_value']}"]],
        node_required=True,
    )
    dep = _deployment_doc(
        p["name"], p["namespace"], p["image"], p["replicas"], labels,
        container_port=80,
        resources={
            "requests": {"cpu": p["cpu_request"], "memory": p["memory_request"]},
            "limits": {"cpu": p["cpu_request"], "memory": p["memory_request"]},
        },
    )
    dep["spec"]["template"]["spec"]["nodeSelector"] = node_selector
    r.assertions = [
        ResourceAssertion("deployments.apps", p["name"], p["namespace"]),
        ResourceAssertion("deployments.apps", p["name"], p["namespace"], "{.spec.replicas}", p["replicas"]),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.spec.template.spec.containers[0].image}", p["image"],
        ),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.spec.template.spec.nodeSelector}", node_selector, "eq",
        ),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.spec.template.spec.containers[0].resources.requests.cpu}", p["cpu_request"],
        ),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.status.availableReplicas}", p["replicas"], "gte",
        ),
    ]
    r.reference_manifests = [dep]
    return r


def render_troubleshooting_crashloop(q: QuestionSpec) -> RenderResult:
    p = q.params
    labels = p["labels"]
    failure = p["failure"]
    name = p["name"]
    namespace = p["namespace"]
    image = p["image"]

    if failure == "bad_liveness":
        broken = _deployment_doc(
            name, namespace, image, p["replicas"], labels,
            container_port=80, liveness_path="/health",
        )
        fixed = _deployment_doc(
            name, namespace, image, p["replicas"], labels,
            container_port=80, liveness_path="/",
        )
    else:  # exit_immediately
        broken = _deployment_doc(
            name, namespace, image, p["replicas"], labels,
            command=["sh", "-c", "exit 1"],
        )
        fixed = _deployment_doc(
            name, namespace, image, p["replicas"], labels,
            command=["sh", "-c", "sleep 3600"],
        )

    task = (
        f"A Deployment named `{name}` in namespace `{namespace}` is unhealthy: its pods keep restarting. "
        f"Investigate the cause and fix the Deployment so that all {p['replicas']} replica(s) become Ready."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace), broken],
    )
    selector = _selector_string(labels)
    r.assertions = [
        ResourceAssertion("deployments.apps", name, namespace),
        ResourceAssertion("deployments.apps", name, namespace, "{.status.availableReplicas}", p["replicas"], "gte"),
        ResourceAssertion("deployments.apps", name, namespace, "{.status.readyReplicas}", p["replicas"], "gte"),
        CountAssertion("pods", namespace, selector, p["replicas"], "gte"),
    ]
    r.reference_manifests = [fixed]
    return r


def render_configmap_secret(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    name = p["name"]
    cm_name = f"{name}-cm"
    secret_name = f"{name}-secret"
    deploy_name = p["deploy_name"]
    labels = p["labels"]
    cm_data = p["cm_data"]
    secret_data = p["secret_data"]

    task = (
        f"In namespace `{namespace}`, create a ConfigMap `{cm_name}` with the data {json.dumps(cm_data)}, "
        f"a Secret `{secret_name}` with the data {json.dumps(secret_data)}, and a Deployment `{deploy_name}` "
        f"(image `{p['image']}`, {p['replicas']} replica(s), labels {_labels_repr(labels)}) that consumes "
        f"both via envFrom."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace)],
    )
    r.assertions = [
        ResourceAssertion("configmaps", cm_name, namespace),
        ResourceAssertion("secrets", secret_name, namespace),
        ResourceAssertion("deployments.apps", deploy_name, namespace),
        ResourceAssertion(
            "deployments.apps", deploy_name, namespace,
            "{.spec.template.spec.containers[0].image}", p["image"],
        ),
        ResourceAssertion(
            "deployments.apps", deploy_name, namespace,
            "{.status.availableReplicas}", p["replicas"], "gte",
        ),
    ]
    for key, value in cm_data.items():
        r.assertions.append(ResourceAssertion("configmaps", cm_name, namespace, f"{{.data.{key}}}", value))
    for key, value in secret_data.items():
        expected = base64.b64encode(value.encode()).decode()
        r.assertions.append(ResourceAssertion("secrets", secret_name, namespace, f"{{.data.{key}}}", expected))

    r.reference_manifests = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": cm_name, "namespace": namespace}, "data": cm_data},
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": secret_name, "namespace": namespace}, "stringData": secret_data},
        _deployment_doc(
            deploy_name, namespace, p["image"], p["replicas"], labels,
            container_port=80,
            resources={"requests": {"cpu": "100m"}},
            volume_mounts=None,
            volumes=None,
        ),
    ]
    r.reference_manifests[-1]["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"configMapRef": {"name": cm_name}},
        {"secretRef": {"name": secret_name}},
    ]
    return r


def render_fix_served_file(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    name = p["name"]
    labels = p["labels"]
    port = p.get("port", 80)
    cm_name = f"{name}-html"
    svc_name = f"{name}-svc"
    probe = f"{name}-probe"
    expected = p["expected_content"]

    task = (
        f"A Deployment `{name}` in namespace `{namespace}` serves a web page from ConfigMap "
        f"`{cm_name}`. The page currently contains the wrong content. Update the ConfigMap so "
        f"the served page contains: `{expected}`."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(namespace),
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": cm_name, "namespace": namespace},
                "data": {"index.html": p["bad_content"]},
            },
            _deployment_doc(
                name, namespace, "nginx:1.27", 1, labels,
                container_port=port,
                volume_mounts=[{"name": "html", "mountPath": "/usr/share/nginx/html"}],
                volumes=[{"name": "html", "configMap": {"name": cm_name}}],
            ),
            _service_doc(svc_name, namespace, "ClusterIP", port, port, labels),
            _pod_doc(probe, namespace, "busybox:1.36", {"role": "probe"}, ["sh", "-c", "sleep 3600"]),
        ],
    )
    r.assertions = [
        ResourceAssertion("configmaps", cm_name, namespace),
        ExecContentAssertion(
            probe, namespace,
            ["wget", "-qO-", "-T", "2", f"http://{svc_name}:{port}/"],
            expect_contains=expected,
        ),
    ]
    r.reference_manifests = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": cm_name, "namespace": namespace},
            "data": {"index.html": expected},
        }
    ]
    return r


def render_cni_config(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    name = p["name"]
    key = p.get("key", "cni-config")
    plugin = p["plugin_type"]
    version = p["cni_version"]

    complete = json.dumps(
        {"cniVersion": version, "name": "k8snet", "plugins": [{"type": plugin, "isDefaultGateway": True}]},
        indent=2,
    )
    incomplete = (
        "{\n"
        f'  "cniVersion": "{version}",\n'
        '  "name": "k8snet",\n'
        "  \"plugins\": [\n"
        '    { "type": "__INCOMPLETE__", "isDefaultGateway": true }\n'
        "  ]\n"
        "}\n"
    )
    file_path = "cni/10-cni.conflist"
    task = (
        f"In namespace `{namespace}`, fix the incomplete CNI config file at `files/{file_path}`: "
        f"complete the plugin entry with `type: {plugin}` and keep `cniVersion: {version}`. Then create "
        f"a ConfigMap `{name}` with key `{key}` containing the corrected file."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace)],
        files=[ExamFile(path=file_path, content=incomplete)],
    )
    r.assertions = [
        ResourceAssertion("configmaps", name, namespace),
        ResourceAssertion("configmaps", name, namespace, f"{{.data.{key}}}", plugin, "contains"),
        ResourceAssertion("configmaps", name, namespace, f"{{.data.{key}}}", version, "contains"),
    ]
    r.reference_manifests = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": namespace},
            "data": {key: complete},
        }
    ]
    return r


def render_autoscaling(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    workload = p["workload"]
    name = p["name"]
    labels = p["labels"]

    task = (
        f"In namespace `{namespace}`, a Deployment `{workload}` exists. Create a "
        f"HorizontalPodAutoscaler named `{name}` with min `{p['min']}` and max `{p['max']}` replicas, "
        f"scaling on CPU at a target of {p['cpu_target']}%."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(namespace),
            _deployment_doc(
                workload, namespace, p["image"], p["replicas"], labels,
                container_port=80,
                resources={"requests": {"cpu": "100m"}, "limits": {"cpu": "500m"}},
            ),
        ],
    )
    r.assertions = [
        ResourceAssertion("horizontalpodautoscalers.autoscaling", name, namespace),
        ResourceAssertion("horizontalpodautoscalers.autoscaling", name, namespace, "{.spec.minReplicas}", p["min"]),
        ResourceAssertion("horizontalpodautoscalers.autoscaling", name, namespace, "{.spec.maxReplicas}", p["max"]),
        ResourceAssertion(
            "horizontalpodautoscalers.autoscaling", name, namespace,
            "{.spec.metrics[0].resource.target.averageUtilization}", p["cpu_target"],
        ),
    ]
    r.reference_manifests = [
        {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": workload,
                },
                "minReplicas": p["min"],
                "maxReplicas": p["max"],
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {"type": "Utilization", "averageUtilization": p["cpu_target"]},
                        },
                    }
                ],
            },
        }
    ]
    return r


_HELM_DEPLOYMENT_TMPL = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}
  namespace: {{ .Release.Namespace }}
  labels:
    app: {{ .Release.Name }}
spec:
  replicas: {{ .Values.replicas }}
  selector:
    matchLabels:
      app: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}
    spec:
      containers:
        - name: app
          image: {{ .Values.image }}
          ports:
            - containerPort: {{ .Values.servicePort }}
"""

_HELM_SERVICE_TMPL = """apiVersion: v1
kind: Service
metadata:
  name: {{ .Release.Name }}-svc
  namespace: {{ .Release.Namespace }}
spec:
  type: ClusterIP
  selector:
    app: {{ .Release.Name }}
  ports:
    - port: {{ .Values.servicePort }}
      targetPort: {{ .Values.servicePort }}
"""


def render_helm(q: QuestionSpec) -> RenderResult:
    p = q.params
    release = p["release"]
    namespace = p["namespace"]
    chart = p.get("chart_name", "myapp")
    image = p["image"]
    replicas = p["replicas"]
    port = p.get("service_port", 80)

    base = f"helm/{chart}"
    files = [
        ExamFile(path=f"{base}/Chart.yaml", content=(
            "apiVersion: v2\n"
            f"name: {chart}\n"
            "description: A Helm chart for the CKA mock exam\n"
            "type: application\n"
            "version: 0.1.0\n"
            "appVersion: \"1.0\"\n"
        )),
        ExamFile(path=f"{base}/values.yaml", content=(
            "image: nginx:1.25\n"
            "replicas: 1\n"
            "servicePort: 80\n"
        )),
        ExamFile(path=f"{base}/templates/deployment.yaml", content=_HELM_DEPLOYMENT_TMPL),
        ExamFile(path=f"{base}/templates/service.yaml", content=_HELM_SERVICE_TMPL),
    ]
    task = (
        f"A Helm chart is provided at `files/{base}`. Using Helm, install it with release name "
        f"`{release}` in namespace `{namespace}`, overriding `image={image}` and `replicas={replicas}`. "
        f"The installed Deployment must be named `{release}` and its Service `{release}-svc`."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace)],
        files=files,
    )
    labels = {"app": release}
    r.assertions = [
        ResourceAssertion("deployments.apps", release, namespace),
        ResourceAssertion(
            "deployments.apps", release, namespace,
            "{.spec.template.spec.containers[0].image}", image,
        ),
        ResourceAssertion("deployments.apps", release, namespace, "{.spec.replicas}", replicas),
        ResourceAssertion("services", f"{release}-svc", namespace),
    ]
    r.reference_manifests = [
        _deployment_doc(release, namespace, image, replicas, labels, container_port=port),
        _service_doc(f"{release}-svc", namespace, "ClusterIP", port, port, labels),
    ]
    return r


_BASE_KUSTOMIZATION = "resources:\n  - deployment.yaml\n"

_BASE_DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
  labels:
    app: app
spec:
  replicas: 1
  selector:
    matchLabels:
      app: app
  template:
    metadata:
      labels:
        app: app
    spec:
      containers:
        - name: app
          image: nginx:1.25
          ports:
            - containerPort: 80
"""


def render_kustomize(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    overlay = p.get("overlay", "prod")
    prefix = p.get("name_prefix", "prod-")
    base_name = p.get("base_name", "app")
    image = p["image"]
    replicas = p["replicas"]
    result_name = f"{prefix}{base_name}"
    labels = {"app": base_name}

    files = [
        ExamFile(path="kustomize/base/kustomization.yaml", content=_BASE_KUSTOMIZATION),
        ExamFile(path="kustomize/base/deployment.yaml", content=_BASE_DEPLOYMENT),
    ]
    task = (
        f"A kustomize base is provided at `files/kustomize/base`. Create an overlay at "
        f"`files/kustomize/overlays/{overlay}` that sets namespace `{namespace}`, sets namePrefix "
        f"`{prefix}`, and patches the image to `{image}` with `{replicas}` replica(s). Apply it with "
        f"`kubectl apply -k files/kustomize/overlays/{overlay}`. The resulting Deployment must be "
        f"`{result_name}` in namespace `{namespace}`."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace)],
        files=files,
    )
    r.assertions = [
        ResourceAssertion("deployments.apps", result_name, namespace),
        ResourceAssertion(
            "deployments.apps", result_name, namespace,
            "{.spec.template.spec.containers[0].image}", image,
        ),
        ResourceAssertion("deployments.apps", result_name, namespace, "{.spec.replicas}", replicas),
        ResourceAssertion(
            "deployments.apps", result_name, namespace,
            "{.status.availableReplicas}", replicas, "gte",
        ),
    ]
    r.reference_manifests = [
        _deployment_doc(result_name, namespace, image, replicas, labels, container_port=80),
    ]
    return r


_INGRESS_CONTROLLER_SVC = "ingress-nginx-controller.ingress-nginx.svc"


def _ingress_probe_cmd(host: str, path: str = "/") -> str:
    return (
        f"wget -qO- -T 10 --header='Host: {host}' "
        f"http://{_INGRESS_CONTROLLER_SVC}{path or '/'}"
    )


def _marker_backend(name, namespace, labels, marker, *, port, image, replicas) -> list[dict]:
    cm_name = f"{name}-html"
    deploy_name = name
    svc_name = f"{name}-svc"
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": cm_name, "namespace": namespace},
            "data": {"index.html": marker},
        },
        _deployment_doc(
            deploy_name, namespace, image, replicas, labels,
            container_port=80,
            volume_mounts=[{"name": "html", "mountPath": "/usr/share/nginx/html"}],
            volumes=[{"name": "html", "configMap": {"name": cm_name}}],
        ),
        _service_doc(svc_name, namespace, "ClusterIP", port, 80, labels),
    ]


def _ingress_rule(host: str, svc_name: str, port: int) -> dict:
    return {
        "host": host,
        "http": {
            "paths": [
                {
                    "path": "/",
                    "pathType": "Prefix",
                    "backend": {"service": {"name": svc_name, "port": {"number": port}}},
                }
            ]
        },
    }


def render_ingress(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    name = p["name"]
    host = p["host"]
    port = p.get("port", 80)
    ingress_class = p.get("ingress_class", "nginx")
    # The marker is served via a ConfigMap mounted at nginx's html root, so the
    # backend image is fixed to nginx.
    backend_image = "nginx:1.27"
    replicas = p.get("replicas", 1)
    labels = p["labels"]
    svc_name = f"{name}-backend-svc"
    deploy_name = f"{name}-backend"
    probe = f"{name}-probe"

    task = (
        f"In namespace `{namespace}`, a backend Deployment (`{deploy_name}`) and Service "
        f"(`{svc_name}`) already exist. Create an Ingress named `{name}` that routes host "
        f"`{host}` (path `/`) to Service `{svc_name}` on port `{port}`, using ingress class "
        f"`{ingress_class}`."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(namespace),
            *_marker_backend(
                deploy_name, namespace, labels, p["marker"],
                port=port, image=backend_image, replicas=replicas,
            ),
            _pod_doc(probe, namespace, "busybox:1.36", {"role": "probe"}, ["sh", "-c", "sleep 3600"]),
        ],
    )
    r.assertions = [
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace),
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace, "{.spec.rules[0].host}", host),
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace, "{.spec.rules[0].http.paths[0].path}", "/"),
        ResourceAssertion(
            "ingresses.networking.k8s.io", name, namespace,
            "{.spec.rules[0].http.paths[0].backend.service.name}", svc_name,
        ),
        ResourceAssertion(
            "ingresses.networking.k8s.io", name, namespace,
            "{.spec.rules[0].http.paths[0].backend.service.port.number}", port,
        ),
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace, "{.spec.ingressClassName}", ingress_class),
        ExecContentAssertion(
            probe, namespace, ["sh", "-c", _ingress_probe_cmd(host)],
            expect_contains=p["marker"],
        ),
    ]
    r.reference_manifests = [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "ingressClassName": ingress_class,
                "rules": [_ingress_rule(host, svc_name, port)],
            },
        }
    ]
    return r


def render_ingress_multi(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    name = p["name"]
    port = p.get("port", 80)
    ingress_class = p.get("ingress_class", "nginx")
    backend_image = "nginx:1.27"
    replicas = p.get("replicas", 1)
    probe = f"{name}-probe"
    svc_a = f"{name}-a-svc"
    svc_b = f"{name}-b-svc"
    deploy_a = f"{name}-a"
    deploy_b = f"{name}-b"

    task = (
        f"In namespace `{namespace}`, two backend Deployments (`{deploy_a}`, `{deploy_b}`) and "
        f"Services (`{svc_a}`, `{svc_b}`) already exist. Create an Ingress named `{name}` with two "
        f"host rules: `{p['host_a']}` -> Service `{svc_a}` and `{p['host_b']}` -> Service `{svc_b}` "
        f"(both path `/`, port `{port}`, ingress class `{ingress_class}`)."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(namespace),
            *_marker_backend(
                deploy_a, namespace, p["labels_a"], p["marker_a"],
                port=port, image=backend_image, replicas=replicas,
            ),
            *_marker_backend(
                deploy_b, namespace, p["labels_b"], p["marker_b"],
                port=port, image=backend_image, replicas=replicas,
            ),
            _pod_doc(probe, namespace, "busybox:1.36", {"role": "probe"}, ["sh", "-c", "sleep 3600"]),
        ],
    )
    r.assertions = [
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace),
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace, "{.spec.rules[0].host}", p["host_a"]),
        ResourceAssertion(
            "ingresses.networking.k8s.io", name, namespace,
            "{.spec.rules[0].http.paths[0].backend.service.name}", svc_a,
        ),
        ResourceAssertion(
            "ingresses.networking.k8s.io", name, namespace,
            "{.spec.rules[0].http.paths[0].backend.service.port.number}", port,
        ),
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace, "{.spec.rules[1].host}", p["host_b"]),
        ResourceAssertion(
            "ingresses.networking.k8s.io", name, namespace,
            "{.spec.rules[1].http.paths[0].backend.service.name}", svc_b,
        ),
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace, "{.spec.ingressClassName}", ingress_class),
        ExecContentAssertion(
            probe, namespace, ["sh", "-c", _ingress_probe_cmd(p["host_a"])],
            expect_contains=p["marker_a"],
        ),
        ExecContentAssertion(
            probe, namespace, ["sh", "-c", _ingress_probe_cmd(p["host_b"])],
            expect_contains=p["marker_b"],
        ),
    ]
    r.reference_manifests = [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "ingressClassName": ingress_class,
                "rules": [
                    _ingress_rule(p["host_a"], svc_a, port),
                    _ingress_rule(p["host_b"], svc_b, port),
                ],
            },
        }
    ]
    return r


def render_crd(q: QuestionSpec) -> RenderResult:
    p = q.params
    plural = p["plural"]
    group = p["group"]
    crd_name = f"{plural}.{group}"
    version = p["version"]
    kind = p["kind"]
    scope = p["scope"]
    spec_field = p["spec_field"]
    spec_type = p["spec_type"]
    instance_name = p["instance_name"]
    instance_ns = p["instance_namespace"] if scope == "Namespaced" else None
    instance_value = p["instance_value"]

    if spec_type == "integer":
        spec_value: object = int(instance_value)
    elif spec_type == "boolean":
        spec_value = instance_value == "true"
    else:
        spec_value = instance_value

    crd_doc = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": crd_name},
        "spec": {
            "group": group,
            "names": {"kind": kind, "plural": plural, "singular": p["singular"]},
            "scope": scope,
            "versions": [
                {
                    "name": version,
                    "served": True,
                    "storage": True,
                    "schema": {
                        "openAPIV3Schema": {
                            "type": "object",
                            "properties": {
                                "spec": {
                                    "type": "object",
                                    "properties": {spec_field: {"type": spec_type}},
                                }
                            },
                            "required": ["spec"],
                        }
                    },
                }
            ],
        },
    }
    instance_doc = {
        "apiVersion": f"{group}/{version}",
        "kind": kind,
        "metadata": {"name": instance_name},
        "spec": {spec_field: spec_value},
    }
    if instance_ns:
        instance_doc["metadata"]["namespace"] = instance_ns

    task = (
        f"The `{crd_name}` custom resource is NOT defined. Create a CustomResourceDefinition "
        f"named `{crd_name}` (group `{group}`, version `{version}`, kind `{kind}`, plural `{plural}`, "
        f"scope `{scope}`) whose schema has a `spec.{spec_field}` field of type `{spec_type}`. Then "
        f"create a `{kind}` instance named `{instance_name}`"
        + (f" in namespace `{instance_ns}`" if instance_ns else "")
        + f" with spec.{spec_field} = `{instance_value}`."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(instance_ns)] if instance_ns else [],
    )
    r.assertions = [
        ResourceAssertion("customresourcedefinitions.apiextensions.k8s.io", crd_name),
        ResourceAssertion("customresourcedefinitions.apiextensions.k8s.io", crd_name, None, "{.spec.group}", group),
        ResourceAssertion("customresourcedefinitions.apiextensions.k8s.io", crd_name, None, "{.spec.names.kind}", kind),
        ResourceAssertion("customresourcedefinitions.apiextensions.k8s.io", crd_name, None, "{.spec.names.plural}", plural),
        ResourceAssertion("customresourcedefinitions.apiextensions.k8s.io", crd_name, None, "{.spec.versions[0].name}", version),
        ResourceAssertion("customresourcedefinitions.apiextensions.k8s.io", crd_name, None, "{.spec.scope}", scope),
        ResourceAssertion(f"{plural}.{group}", instance_name, instance_ns),
        ResourceAssertion(f"{plural}.{group}", instance_name, instance_ns, f"{{.spec.{spec_field}}}", instance_value),
    ]
    r.reference_manifests = [crd_doc, instance_doc]
    return r


def render_operator(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    cert_name = p["cert_name"]
    issuer_name = p["issuer_name"]
    host = p["host"]
    # Pinned official cert-manager static install manifest (downloaded once,
    # cached, and placed in the workdir as a local file).
    cert_manager_url = (
        "https://github.com/cert-manager/cert-manager/releases/download/"
        "v1.21.1/cert-manager.yaml"
    )

    task = (
        f"cert-manager is NOT installed. Install it from the provided local file "
        f"`files/cert-manager/install.yaml`:\n"
        f"    kubectl apply -f files/cert-manager/install.yaml\n"
        f"Then create a self-signed `Issuer` named `{issuer_name}` in namespace `{namespace}` and a "
        f"`Certificate` named `{cert_name}` for host `{host}`. The Certificate must become Ready "
        f"and produce a TLS Secret."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace)],
        files=[ExamFile(path="cert-manager/install.yaml", content=fetch_manifest(cert_manager_url))],
        reference_install_commands=[["apply", "-f", f"{FILES_TOKEN}/cert-manager/install.yaml"]],
        reference_ready_assertions=[
            ResourceAssertion("deployments.apps", "cert-manager-webhook", "cert-manager", "{.status.availableReplicas}", 1, "gte"),
            ResourceAssertion("deployments.apps", "cert-manager", "cert-manager", "{.status.availableReplicas}", 1, "gte"),
        ],
        reference_teardown_commands=[
            ["delete", "certificates.cert-manager.io", cert_name, "-n", namespace, "--ignore-not-found"],
            ["delete", "issuers.cert-manager.io", issuer_name, "-n", namespace, "--ignore-not-found"],
            ["delete", "ns", "cert-manager", "--ignore-not-found"],
            ["delete", "validatingwebhookconfiguration", "cert-manager-webhook", "--ignore-not-found"],
            ["delete", "mutatingwebhookconfiguration", "cert-manager-webhook", "--ignore-not-found"],
            ["delete", "crd",
             "certificates.cert-manager.io", "issuers.cert-manager.io", "clusterissuers.cert-manager.io",
             "certificaterequests.cert-manager.io", "orders.acme.cert-manager.io", "challenges.acme.cert-manager.io",
             "--ignore-not-found"],
        ],
    )
    r.reference_manifests = [
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Issuer",
            "metadata": {"name": issuer_name, "namespace": namespace},
            "spec": {"selfSigned": {}},
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": cert_name, "namespace": namespace},
            "spec": {
                "secretName": cert_name,
                "issuerRef": {"name": issuer_name, "kind": "Issuer"},
                "dnsNames": [host],
            },
        },
    ]
    r.assertions = [
        ResourceAssertion("issuers.cert-manager.io", issuer_name, namespace),
        ResourceAssertion(
            "issuers.cert-manager.io", issuer_name, namespace,
            '{.status.conditions[?(@.type=="Ready")].status}', "True",
        ),
        ResourceAssertion("certificates.cert-manager.io", cert_name, namespace),
        ResourceAssertion(
            "certificates.cert-manager.io", cert_name, namespace,
            '{.status.conditions[?(@.type=="Ready")].status}', "True",
        ),
        ResourceAssertion("secrets", cert_name, namespace),
    ]
    return r


def render_gateway(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    name = p["name"]
    host = p["host"]
    port = p.get("port", 80)
    replicas = p.get("replicas", 1)
    labels = p["labels"]
    svc_name = f"{name}-backend-svc"
    deploy_name = f"{name}-backend"
    gateway_class = "contour"
    # Contour is installed in gateway-ref mode: it serves the fixed `contour`
    # Gateway in the `projectcontour` namespace (created by the install).
    gateway_name = "contour"
    gateway_namespace = "projectcontour"
    # Pinned official Contour Gateway API install. The shipped `example`
    # GatewayClass is stripped and the `contour` Gateway is repointed at the
    # GatewayClass the candidate will create.
    contour_url = (
        "https://raw.githubusercontent.com/projectcontour/contour/v1.33.6/"
        "examples/render/contour-gateway.yaml"
    )

    task = (
        f"The Gateway API is NOT installed. Install it from the provided local file "
        f"`files/gateway/contour-gateway.yaml`:\n"
        f"    kubectl apply -f files/gateway/contour-gateway.yaml\n"
        f"(if it reports 'resource mapping not found ... ensure CRDs are installed first', "
        f"simply run the apply again — the first pass registers the CRDs.)\n"
        f"This installs the Gateway API CRDs, the Contour controller, the Envoy data plane, and a "
        f"Gateway `{gateway_name}` in namespace `{gateway_namespace}`. Then create a GatewayClass "
        f"named `{gateway_class}` (controllerName `projectcontour.io/gateway-controller`) and an "
        f"HTTPRoute named `{name}` in namespace `{namespace}` attached to the `{gateway_name}` "
        f"Gateway, routing host `{host}` to Service `{svc_name}` on port {port}."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(namespace),
            _deployment_doc(deploy_name, namespace, "nginx:1.27", replicas, labels, container_port=80),
            _service_doc(svc_name, namespace, "ClusterIP", port, 80, labels),
        ],
        files=[
            ExamFile(
                path="gateway/contour-gateway.yaml",
                content=rewrite_contour_manifest(fetch_manifest(contour_url), gateway_class),
            ),
        ],
        reference_install_commands=[
            ["apply", "-f", f"{FILES_TOKEN}/gateway/contour-gateway.yaml"],
        ],
        reference_ready_assertions=[
            ResourceAssertion("deployments.apps", "contour", "projectcontour", "{.status.availableReplicas}", 1, "gte"),
        ],
        reference_teardown_commands=[
            ["delete", "httproutes.gateway.networking.k8s.io", name, "-n", namespace, "--ignore-not-found"],
            ["delete", "gatewayclasses.gateway.networking.k8s.io", gateway_class, "--ignore-not-found"],
            ["delete", "ns", "projectcontour", "--ignore-not-found"],
            ["delete", "crd",
             "gateways.gateway.networking.k8s.io", "gatewayclasses.gateway.networking.k8s.io",
             "httproutes.gateway.networking.k8s.io", "referencegrants.gateway.networking.k8s.io",
             "--ignore-not-found"],
        ],
    )
    gatewayclass_doc = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "GatewayClass",
        "metadata": {"name": gateway_class},
        "spec": {"controllerName": "projectcontour.io/gateway-controller"},
    }
    httproute_doc = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "parentRefs": [{"name": gateway_name, "namespace": gateway_namespace}],
            "hostnames": [host],
            "rules": [{"backendRefs": [{"name": svc_name, "port": port}]}],
        },
    }
    r.assertions = [
        ResourceAssertion("customresourcedefinitions.apiextensions.k8s.io", "gatewayclasses.gateway.networking.k8s.io"),
        ResourceAssertion("customresourcedefinitions.apiextensions.k8s.io", "httproutes.gateway.networking.k8s.io"),
        ResourceAssertion("gatewayclasses.gateway.networking.k8s.io", gateway_class),
        ResourceAssertion(
            "gatewayclasses.gateway.networking.k8s.io", gateway_class, None,
            "{.spec.controllerName}", "projectcontour.io/gateway-controller",
        ),
        ResourceAssertion("gateways.gateway.networking.k8s.io", gateway_name, gateway_namespace),
        ResourceAssertion(
            "gateways.gateway.networking.k8s.io", gateway_name, gateway_namespace,
            '{.status.conditions[?(@.type=="Accepted")].status}', "True",
        ),
        ResourceAssertion(
            "gateways.gateway.networking.k8s.io", gateway_name, gateway_namespace,
            '{.status.conditions[?(@.type=="Programmed")].status}', "True",
        ),
        ResourceAssertion("httproutes.gateway.networking.k8s.io", name, namespace),
        ResourceAssertion("httproutes.gateway.networking.k8s.io", name, namespace, "{.spec.hostnames[0]}", host),
        ResourceAssertion(
            "httproutes.gateway.networking.k8s.io", name, namespace,
            "{.spec.rules[0].backendRefs[0].name}", svc_name,
        ),
        ResourceAssertion(
            "httproutes.gateway.networking.k8s.io", name, namespace,
            "{.spec.rules[0].backendRefs[0].port}", port,
        ),
        ResourceAssertion(
            "httproutes.gateway.networking.k8s.io", name, namespace,
            "{.spec.parentRefs[0].name}", gateway_name,
        ),
    ]
    r.reference_manifests = [gatewayclass_doc, httproute_doc]
    return r


RENDERERS = {
    "deployment": render_deployment,
    "service": render_service,
    "networkpolicy": render_networkpolicy,
    "rbac": render_rbac,
    "pvc": render_pvc,
    "scheduling": render_scheduling,
    "troubleshooting_crashloop": render_troubleshooting_crashloop,
    "configmap_secret": render_configmap_secret,
    "fix_served_file": render_fix_served_file,
    "cni_config": render_cni_config,
    "autoscaling": render_autoscaling,
    "helm": render_helm,
    "kustomize": render_kustomize,
    "ingress": render_ingress,
    "ingress_multi": render_ingress_multi,
    "crd": render_crd,
    "operator": render_operator,
    "gateway": render_gateway,
}


def render_exam(plan) -> list[RenderResult]:
    """Render every question of a plan into concrete, verifiable steps."""
    results: list[RenderResult] = []
    for index, question in enumerate(plan.questions, start=1):
        renderer = RENDERERS.get(question.archetype_id)
        if renderer is None:
            raise ValueError(f"no renderer for archetype {question.archetype_id}")
        result = renderer(question)
        result.question_index = index
        results.append(result)
    return results


def render_task_markdown(results: list[RenderResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"### Q{r.question_index} [{r.archetype_id}]")
        lines.append(r.task)
        if r.files:
            lines.append("")
            lines.append("Files provided (in the exam workdir):")
            for f in r.files:
                lines.append(f"- `{f.path}`")
        lines.append("")
    return "\n".join(lines)
