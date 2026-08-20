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
    ApplyFailsAssertion,
    Assertion,
    CountAssertion,
    ExecAssertion,
    ExecContentAssertion,
    LiveQueryMatchAssertion,
    ResourceAssertion,
)
from .manifests import fetch_manifest, rewrite_contour_manifest
from .schemas import QuestionSpec

from ._csr_ref import CERT_PEM_HEADER_B64, PRIV_KEY_PEM_HEADER_B64, REF_CSR_B64, REF_PRIVATE_KEY

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
    # Optional raw shell commands run with KUBECONFIG set, after any regular
    # reference commands. Used when the reference must capture kubectl output
    # into an artifact (e.g. decoding a signed certificate or seeding a
    # ConfigMap with a query's stdout) before assertions run.
    reference_shell_commands: list[str] = field(default_factory=list)
    # Setup objects that are intentionally broken (e.g. a Deployment the candidate
    # must fix). Setup-phase readiness waits must skip them: waiting would block
    # for the full timeout on a resource that is *supposed* to be unhealthy.
    setup_waits_skip: set[tuple[str, str]] = field(default_factory=set)


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
        setup_waits_skip={("Deployment", name)},
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


def render_csr(q: QuestionSpec) -> RenderResult:
    p = q.params
    csr_name = p["csr_name"]
    cn = p["cn"]
    namespace = p["namespace"]
    secret_name = p["secret_name"]
    signer_name = "kubernetes.io/kube-apiserver-client"

    task = (
        f"Generate a private key and a certificate signing request for the user CN `{cn}`:\n"
        f"    openssl genrsa -out user.key 2048\n"
        f"    openssl req -new -key user.key -subj /CN={cn} -out user.csr\n"
        f"Create a CertificateSigningRequest named `{csr_name}` with that request:\n"
        f"    kubectl apply -f - <<EOF\n"
        f"apiVersion: certificates.k8s.io/v1\n"
        f"kind: CertificateSigningRequest\n"
        f"metadata:\n"
        f"  name: {csr_name}\n"
        f"spec:\n"
        f"  request: $(base64 -w0 user.csr)\n"
        f"  signerName: {signer_name}\n"
        f"  usages:\n"
        f"    - client auth\n"
        f"EOF\n"
        f"Approve the request:\n"
        f"    kubectl certificate approve {csr_name}\n"
        f"The cluster signs it with the CA. Extract the issued certificate:\n"
        f"    kubectl get csr {csr_name} -o jsonpath='{{.status.certificate}}' | base64 -d > user.crt\n"
        f"Finally, create a TLS Secret named `{secret_name}` in namespace `{namespace}` containing "
        f"the key and certificate:\n"
        f"    kubectl create secret tls {secret_name} -n {namespace} --cert=user.crt --key=user.key\n"
    )

    key_pem = REF_PRIVATE_KEY
    # Shell commands for the reference solution: approve the request, wait for the
    # CA to sign it, decode the certificate, and create the TLS Secret.
    shell_wait = (
        "for i in $(seq 1 60); do "
        f"CERT=$(kubectl get csr {csr_name} -o jsonpath={{.status.certificate}} 2>/dev/null || true); "
        "[ -n \"$CERT\" ] && break; sleep 2; done; "
        "if [ -z \"${CERT:-}\" ]; then echo 'CSR was not signed in time' >&2; exit 1; fi"
    )
    shell_create_secret = (
        f"printf '%s\\n' '{key_pem}' > /tmp/cka_ref_user.key\n"
        f"kubectl get csr {csr_name} -o jsonpath={{.status.certificate}} | base64 -d > /tmp/cka_ref_user.crt\n"
        f"kubectl create secret tls {secret_name} -n {namespace} "
        f"--cert=/tmp/cka_ref_user.crt --key=/tmp/cka_ref_user.key\n"
    )

    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace)],
        reference_manifests=[
            {
                "apiVersion": "certificates.k8s.io/v1",
                "kind": "CertificateSigningRequest",
                "metadata": {"name": csr_name},
                "spec": {
                    "request": REF_CSR_B64,
                    "signerName": signer_name,
                    "usages": ["client auth"],
                },
            },
        ],
        reference_commands=[["certificate", "approve", csr_name]],
        reference_shell_commands=[
            shell_wait + "\n" + shell_create_secret,
        ],
        reference_teardown_commands=[
            ["delete", "secret", secret_name, "-n", namespace, "--ignore-not-found"],
            ["delete", "certificatesigningrequests.certificates.k8s.io", csr_name, "--ignore-not-found"],
        ],
    )
    r.assertions = [
        ResourceAssertion("certificatesigningrequests.certificates.k8s.io", csr_name),
        ResourceAssertion(
            "certificatesigningrequests.certificates.k8s.io", csr_name, None,
            '{.status.conditions[?(@.type=="Approved")].status}', "True",
        ),
        ResourceAssertion(
            "certificatesigningrequests.certificates.k8s.io", csr_name, None,
            "{.status.certificate}", op="nonempty",
        ),
        ResourceAssertion("secrets", secret_name, namespace, "{.data.tls\\.crt}", CERT_PEM_HEADER_B64, "contains"),
        ResourceAssertion("secrets", secret_name, namespace, "{.data.tls\\.key}", PRIV_KEY_PEM_HEADER_B64, "contains"),
    ]
    return r


def render_validating_admission_policy(q: QuestionSpec) -> RenderResult:
    p = q.params
    policy_name = p["policy_name"]
    binding_name = p["binding_name"]
    namespace = p["namespace"]
    label_key = p["label_key"]

    expression = f"has(object.metadata.labels) && '{label_key}' in object.metadata.labels"
    task = (
        f"Create a ValidatingAdmissionPolicy named `{policy_name}` that rejects any Pod CREATE or "
        f"UPDATE in namespace `{namespace}` unless the pod carries the label `{label_key}` (any value). "
        f"Then create a ValidatingAdmissionPolicyBinding named `{binding_name}` that binds the policy "
        f"and applies only to namespace `{namespace}`.\n\n"
        f"The policy must use a CEL expression equivalent to: {expression}\n"
        f"If the rule is working, an attempt to create a pod without that label is rejected by the API server."
    )

    policy_doc = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": policy_name},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {
                "resourceRules": [
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["CREATE", "UPDATE"],
                        "resources": ["pods"],
                    }
                ]
            },
            "validations": [
                {
                    "expression": expression,
                    "message": f"pod must carry the label {label_key}",
                }
            ],
        },
    }
    binding_doc = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": binding_name},
        "spec": {
            "policyName": policy_name,
            "validationActions": ["Deny"],
            "matchResources": {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": namespace},
                }
            },
        },
    }
    violating_pod_name = f"{policy_name}-probe"
    violating_pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": violating_pod_name, "namespace": namespace},
        "spec": {"containers": [{"name": "app", "image": "nginx:1.27"}]},
    }

    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace)],
        reference_manifests=[policy_doc, binding_doc],
        reference_teardown_commands=[
            ["delete", "validatingadmissionpolicybindings.admissionregistration.k8s.io", binding_name, "--ignore-not-found"],
            ["delete", "validatingadmissionpolicies.admissionregistration.k8s.io", policy_name, "--ignore-not-found"],
            ["delete", "pod", violating_pod_name, "-n", namespace, "--ignore-not-found"],
        ],
    )
    r.assertions = [
        ResourceAssertion("validatingadmissionpolicies.admissionregistration.k8s.io", policy_name),
        ResourceAssertion(
            "validatingadmissionpolicies.admissionregistration.k8s.io", policy_name, None,
            "{.spec.validations[0].expression}", expression,
        ),
        ResourceAssertion("validatingadmissionpolicybindings.admissionregistration.k8s.io", binding_name),
        ResourceAssertion(
            "validatingadmissionpolicybindings.admissionregistration.k8s.io", binding_name, None,
            "{.spec.policyName}", policy_name,
        ),
        ApplyFailsAssertion(
            violating_pod,
            description=f"pod without label {label_key} is rejected by the admission policy",
            pre_argv=["delete", "pod", violating_pod_name, "-n", namespace, "--ignore-not-found"],
        ),
    ]
    return r


def render_jsonpath(q: QuestionSpec) -> RenderResult:
    p = q.params
    cm_name = p["cm_name"]
    cm_namespace = p["cm_namespace"]
    cm_key = p["cm_key"]
    query = p["query"]

    queries = {
        "node_names": (
            "{.items[*].metadata.name}",
            "the name of every node",
        ),
        "node_internal_ips": (
            '{.items[*].status.addresses[?(@.type=="InternalIP")].address}',
            "the InternalIP address of every node",
        ),
        "node_os_image": (
            "{.items[*].status.nodeInfo.osImage}",
            "the OS image of every node",
        ),
    }
    jsonpath, description = queries[query]
    canonical_argv = ["get", "nodes", "-o", f"jsonpath={jsonpath}"]
    rendered_query = "kubectl " + " ".join(list(canonical_argv))
    # The reference runs the same query and stores the existing output in the ConfigMap
    # via dry-run + apply (idempotent across preflight/solve cycles). The grader re-runs
    # the canonical query at grade time and compares (whitespace normalized), so the value
    # is verified against the live cluster.
    shell_command = (
        f'kubectl get nodes --no-headers -o jsonpath=\'{jsonpath}\' > /tmp/cka_jp.out 2>/dev/null; '
        f"kubectl create configmap {cm_name} -n {cm_namespace} "
        f"--from-file={cm_key}=/tmp/cka_jp.out --dry-run=client -o yaml | kubectl apply -f -"
    )

    task = (
        f"Run the following kubectl command to extract {description}:\n\n"
        f"    {rendered_query}\n\n"
        f"Store the exact output in a ConfigMap named `{cm_name}` in namespace `{cm_namespace}` "
        f"under the key `{cm_key}`:\n"
        f"    kubectl create configmap {cm_name} -n {cm_namespace} --from-literal={cm_key}="
        f"$(kubectl get nodes -o jsonpath='{jsonpath}')\n\n"
        f"The grader compares the stored value against a fresh run of the same query, so capture "
        f"the output verbatim."
    )

    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(cm_namespace)],
        reference_manifests=[
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": cm_name, "namespace": cm_namespace},
                "data": {cm_key: ""},
            },
        ],
        reference_shell_commands=[shell_command],
        reference_teardown_commands=[
            ["delete", "configmap", cm_name, "-n", cm_namespace, "--ignore-not-found"],
        ],
    )
    r.assertions = [
        ResourceAssertion("configmaps", cm_name, cm_namespace),
        LiveQueryMatchAssertion(
            canonical_argv=canonical_argv,
            stored_resource="configmaps",
            stored_name=cm_name,
            stored_namespace=cm_namespace,
            stored_jsonpath=f"{{.data.{cm_key}}}",
        ),
    ]
    return r


def render_fix_deployment(q: QuestionSpec) -> RenderResult:
    p = q.params
    labels = p["labels"]
    broken = _deployment_doc(
        p["name"], p["namespace"], p["wrong_image"], p["replicas"], labels,
        container_port=p.get("container_port"),
    )
    fixed = _deployment_doc(
        p["name"], p["namespace"], p["image"], p["replicas"], labels,
        container_port=p.get("container_port"),
    )
    task = (
        f"A Deployment named `{p['name']}` in namespace `{p['namespace']}` is running the wrong "
        f"image. Fix it so it runs `{p['image']}` with {p['replicas']} replica(s) and pod labels "
        f"{_labels_repr(labels)}, and all pods become Ready."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(p["namespace"]), broken],
    )
    r.assertions = [
        ResourceAssertion("deployments.apps", p["name"], p["namespace"]),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.spec.template.spec.containers[0].image}", p["image"],
        ),
        ResourceAssertion("deployments.apps", p["name"], p["namespace"], "{.spec.replicas}", p["replicas"]),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.spec.template.metadata.labels}", labels, "superset",
        ),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.status.availableReplicas}", p["replicas"], "gte",
        ),
    ]
    r.reference_manifests = [fixed]
    return r


def render_fix_service(q: QuestionSpec) -> RenderResult:
    p = q.params
    backend = f"{p['name']}-backend"
    labels = p["backend_labels"]
    target_port = p["target_port"]
    backend_image = p.get("backend_image", "nginx:1.27")
    backend_replicas = p.get("backend_replicas", 1)
    broken = _service_doc(p["name"], p["namespace"], p["service_type"], p["port"], target_port, p["wrong_labels"])
    fixed = _service_doc(p["name"], p["namespace"], p["service_type"], p["port"], target_port, labels)
    task = (
        f"In namespace `{p['namespace']}`, a Service `{p['name']}` exists but its selector does "
        f"not match the backend pods (labels {_labels_repr(labels)}), so it has no endpoints. "
        f"Fix the Service selector so it routes to the backend (type `{p['service_type']}`, port "
        f"{p['port']} -> {target_port})."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(p["namespace"]),
            _deployment_doc(backend, p["namespace"], backend_image, backend_replicas, labels, container_port=target_port),
            broken,
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
    r.reference_manifests = [fixed]
    return r


def render_fix_pvc(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    name = p["name"]
    storage_class = p["storage_class"]
    broken = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "accessModes": [p["access_mode"]],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": p["size"]}},
        },
    }
    fixed_sc = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": storage_class},
        "provisioner": "k8s.io/minikube-hostpath",
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "Immediate",
    }
    task = (
        f"A PersistentVolumeClaim `{name}` in namespace `{namespace}` is stuck Pending because "
        f"the StorageClass `{storage_class}` it references does not exist. Create the "
        f"StorageClass `{storage_class}` (with a working provisioner) so the PVC binds "
        f"(access mode `{p['access_mode']}`, {p['size']})."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace), broken],
        setup_waits_skip={("PersistentVolumeClaim", name)},
    )
    r.assertions = [
        ResourceAssertion("persistentvolumeclaims", name, namespace),
        ResourceAssertion("persistentvolumeclaims", name, namespace, "{.spec.storageClassName}", storage_class),
        ResourceAssertion("persistentvolumeclaims", name, namespace, "{.spec.accessModes[0]}", p["access_mode"]),
        ResourceAssertion("persistentvolumeclaims", name, namespace, "{.status.phase}", "Bound"),
        # The candidate's action is creating the missing StorageClass; checking it
        # keeps the challenge "unsolved" after restore (deleting the SC), even
        # though a bound PVC stays bound.
        ResourceAssertion("storageclasses.storage.k8s.io", storage_class),
    ]
    r.reference_manifests = [fixed_sc]
    return r


def render_fix_networkpolicy(q: QuestionSpec) -> RenderResult:
    p = q.params
    name = p["name"]
    namespace = p["namespace"]
    port = p["port"]
    protocol = p.get("protocol", "TCP")
    target = f"{name}-target"
    target_svc = f"{name}-svc"
    peer = f"{name}-peer"
    blocked = f"{name}-blocked"
    target_labels = p["target_labels"]
    peer_labels = p["peer_labels"]
    blocked_labels = p["blocked_labels"]

    def policy_doc(allowed_labels: dict) -> dict:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "podSelector": {"matchLabels": target_labels},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": allowed_labels}}],
                        "ports": [{"protocol": protocol, "port": port}],
                    }
                ],
            },
        }

    broken = policy_doc(blocked_labels)
    fixed = policy_doc(peer_labels)
    task = (
        f"In namespace `{namespace}`, a NetworkPolicy `{name}` exists but is misconfigured: the "
        f"client pod (labels {_labels_repr(peer_labels)}) cannot reach the target (labels "
        f"{_labels_repr(target_labels)}) on port {port}/{protocol}, while a pod that must be "
        f"denied can. Fix the policy so the peer can reach the target and the blocked pod cannot."
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
            _pod_doc(blocked, namespace, "busybox:1.36", blocked_labels, ["sh", "-c", "sleep 3600"]),
            broken,
        ],
    )
    probe = ["wget", "-qO-", "-T", "2", f"http://{target_svc}:{port}/"]
    r.assertions = [
        ResourceAssertion("networkpolicies.networking.k8s.io", name, namespace),
        ExecAssertion(peer, namespace, probe, expect_rc=0),
        ExecAssertion(blocked, namespace, probe, expect_rc=0, op="ne"),
    ]
    r.reference_manifests = [fixed]
    return r


def render_fix_scheduling(q: QuestionSpec) -> RenderResult:
    p = q.params
    labels = p["labels"]
    correct_selector = {p["node_label_key"]: p["node_label_value"]}
    wrong_selector = {"ckamock-nonexistent-key": "nowhere"}
    broken = _deployment_doc(p["name"], p["namespace"], p["image"], p["replicas"], labels, container_port=80)
    broken["spec"]["template"]["spec"]["nodeSelector"] = wrong_selector
    fixed = _deployment_doc(p["name"], p["namespace"], p["image"], p["replicas"], labels, container_port=80)
    fixed["spec"]["template"]["spec"]["nodeSelector"] = correct_selector
    task = (
        f"A Deployment `{p['name']}` in namespace `{p['namespace']}` is stuck Pending because its "
        f"nodeSelector does not match the labeled node (`{p['node_label_key']}={p['node_label_value']}`). "
        f"Fix the nodeSelector so the pods schedule and become Ready."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(p["namespace"]), broken],
        setup_waits_skip={("Deployment", p["name"])},
        setup_commands=[["label", "nodes", NODE_TOKEN, f"{p['node_label_key']}={p['node_label_value']}"]],
        node_required=True,
    )
    r.assertions = [
        ResourceAssertion("deployments.apps", p["name"], p["namespace"]),
        ResourceAssertion("deployments.apps", p["name"], p["namespace"], "{.spec.replicas}", p["replicas"]),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.spec.template.spec.nodeSelector}", correct_selector, "eq",
        ),
        ResourceAssertion(
            "deployments.apps", p["name"], p["namespace"],
            "{.status.availableReplicas}", p["replicas"], "gte",
        ),
    ]
    r.reference_manifests = [fixed]
    return r


def render_fix_configmap(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    cm_name = p["name"]
    deploy_name = p["deploy_name"]
    labels = p["labels"]
    env_key = p["env_key"]
    broken_cm = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": cm_name, "namespace": namespace},
        "data": {env_key: p["wrong_value"]},
    }
    fixed_cm = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": cm_name, "namespace": namespace},
        "data": {env_key: p["correct_value"]},
    }
    deployment = _deployment_doc(deploy_name, namespace, p["image"], p["replicas"], labels, container_port=80)
    deployment["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [{"configMapRef": {"name": cm_name}}]
    task = (
        f"A Deployment `{deploy_name}` in namespace `{namespace}` reads configuration from "
        f"ConfigMap `{cm_name}` (via envFrom). One value is wrong: key `{env_key}` should be "
        f"`{p['correct_value']}`. Update the ConfigMap so it contains the correct value."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[_namespace_doc(namespace), broken_cm, deployment],
    )
    r.assertions = [
        ResourceAssertion("configmaps", cm_name, namespace),
        ResourceAssertion("configmaps", cm_name, namespace, f"{{.data.{env_key}}}", p["correct_value"]),
        ResourceAssertion("deployments.apps", deploy_name, namespace),
        ResourceAssertion(
            "deployments.apps", deploy_name, namespace,
            "{.status.availableReplicas}", p["replicas"], "gte",
        ),
    ]
    r.reference_manifests = [fixed_cm]
    return r


def render_fix_ingress(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    name = p["name"]
    host = p["host"]
    port = p.get("port", 80)
    replicas = p.get("replicas", 1)
    labels = p["labels"]
    marker = p["marker"]
    svc_name = f"{name}-backend-svc"
    deploy_name = f"{name}-backend"
    probe = f"{name}-probe"
    ingress_class = "nginx"

    def ingress_doc(backend_svc: str) -> dict:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "ingressClassName": ingress_class,
                "rules": [{
                    "host": host,
                    "http": {"paths": [{
                        "path": "/", "pathType": "Prefix",
                        "backend": {"service": {"name": backend_svc, "port": {"number": port}}},
                    }]},
                }],
            },
        }

    wrong_svc = f"{name}-missing-svc"
    broken = ingress_doc(wrong_svc)
    fixed = ingress_doc(svc_name)
    task = (
        f"An Ingress `{name}` in namespace `{namespace}` exists for host `{host}` but routes to "
        f"the wrong backend (Service `{wrong_svc}` does not exist). Fix it so it routes to "
        f"Service `{svc_name}` on port {port} and serves the expected content."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(namespace),
            *_marker_backend(deploy_name, namespace, labels, marker, port=port, image="nginx:1.27", replicas=replicas),
            broken,
            _pod_doc(probe, namespace, "busybox:1.36", {"role": "probe"}, ["sh", "-c", "sleep 3600"]),
        ],
    )
    r.assertions = [
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace),
        ResourceAssertion("ingresses.networking.k8s.io", name, namespace, "{.spec.rules[0].host}", host),
        ResourceAssertion(
            "ingresses.networking.k8s.io", name, namespace,
            "{.spec.rules[0].http.paths[0].backend.service.name}", svc_name,
        ),
        ExecContentAssertion(
            probe, namespace, ["sh", "-c", _ingress_probe_cmd(host)],
            expect_contains=marker,
        ),
    ]
    r.reference_manifests = [fixed]
    return r


def render_fix_rbac(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    sa_name = p["sa_name"]
    role_name = p["role_name"]
    role_kind = p["role_kind"]
    cluster_scoped = role_kind == "ClusterRole"
    binding_name = f"{role_name}-binding"
    binding_ns = None if cluster_scoped else namespace

    def role_doc(resources: list[str], verbs: list[str]) -> dict:
        metadata: dict = {"name": role_name}
        if not cluster_scoped:
            metadata["namespace"] = namespace
        return {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": role_kind,
            "metadata": metadata,
            "rules": [{"apiGroups": [""], "resources": resources, "verbs": verbs}],
        }

    wrong_role = role_doc(p["wrong_resources"], p["wrong_verbs"])
    fixed_role = role_doc(p["resources"], p["verbs"])
    binding_metadata: dict = {"name": binding_name}
    if not cluster_scoped:
        binding_metadata["namespace"] = namespace
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding" if cluster_scoped else "RoleBinding",
        "metadata": binding_metadata,
        "subjects": [{"kind": "ServiceAccount", "name": sa_name, "namespace": namespace}],
        "roleRef": {"kind": role_kind, "name": role_name, "apiGroup": "rbac.authorization.k8s.io"},
    }
    role_resource = "clusterroles.rbac.authorization.k8s.io" if cluster_scoped else "roles.rbac.authorization.k8s.io"
    binding_resource = (
        "clusterrolebindings.rbac.authorization.k8s.io" if cluster_scoped
        else "rolebindings.rbac.authorization.k8s.io"
    )
    task = (
        f"In namespace `{namespace}`, a `{role_kind}` `{role_name}` grants the wrong permissions "
        f"to ServiceAccount `{sa_name}`. Fix the Role so it grants exactly the verbs {p['verbs']} "
        f"on resources {p['resources']}."
    )
    r = RenderResult(
        archetype_id=q.archetype_id,
        question_index=0,
        task=task,
        setup_manifests=[
            _namespace_doc(namespace),
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": sa_name, "namespace": namespace}},
            wrong_role,
            binding,
        ],
    )
    r.assertions = [
        ResourceAssertion("serviceaccounts", sa_name, namespace),
        ResourceAssertion(role_resource, role_name, binding_ns),
        ResourceAssertion(role_resource, role_name, binding_ns, "{.rules[*].resources[*]}", p["resources"], "superset"),
        ResourceAssertion(role_resource, role_name, binding_ns, "{.rules[*].verbs[*]}", p["verbs"], "superset"),
        ResourceAssertion(binding_resource, binding_name, binding_ns),
        ResourceAssertion(binding_resource, binding_name, binding_ns, "{.subjects[*].name}", sa_name, "contains"),
    ]
    r.reference_manifests = [fixed_role]
    return r


def render_fix_autoscaling(q: QuestionSpec) -> RenderResult:
    p = q.params
    namespace = p["namespace"]
    workload = p["workload"]
    name = p["name"]
    labels = p["labels"]

    def hpa_doc(min_replicas: int, max_replicas: int) -> dict:
        return {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": workload},
                "minReplicas": min_replicas,
                "maxReplicas": max_replicas,
                "metrics": [{
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {"type": "Utilization", "averageUtilization": p["cpu_target"]},
                    },
                }],
            },
        }

    broken = hpa_doc(p["wrong_min"], p["wrong_max"])
    fixed = hpa_doc(p["min"], p["max"])
    task = (
        f"A HorizontalPodAutoscaler `{name}` for Deployment `{workload}` in namespace "
        f"`{namespace}` has the wrong min/max replicas. Fix it so min is `{p['min']}`, max is "
        f"`{p['max']}`, and it scales on CPU at {p['cpu_target']}%."
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
            broken,
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
    r.reference_manifests = [fixed]
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
    "csr": render_csr,
    "validating_admission_policy": render_validating_admission_policy,
    "jsonpath": render_jsonpath,
    "fix_deployment": render_fix_deployment,
    "fix_service": render_fix_service,
    "fix_pvc": render_fix_pvc,
    "fix_networkpolicy": render_fix_networkpolicy,
    "fix_scheduling": render_fix_scheduling,
    "fix_configmap": render_fix_configmap,
    "fix_ingress": render_fix_ingress,
    "fix_rbac": render_fix_rbac,
    "fix_autoscaling": render_fix_autoscaling,
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
