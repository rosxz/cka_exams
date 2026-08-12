from __future__ import annotations

from cka_mock.archetypes import REGISTRY
from cka_mock.renderer import RENDERERS, render_exam, render_task_markdown
from cka_mock.schemas import ExamPlan, QuestionSpec

VALID_PARAMS = {
    "deployment": {
        "name": "web",
        "namespace": "frontend",
        "image": "nginx:1.27",
        "replicas": 3,
        "labels": {"app": "web"},
        "container_port": 80,
    },
    "service": {
        "name": "web-svc",
        "namespace": "frontend",
        "service_type": "NodePort",
        "port": 80,
        "target_port": 8080,
        "backend_labels": {"app": "web"},
    },
    "networkpolicy": {
        "name": "allow-api",
        "namespace": "api",
        "target_labels": {"app": "api"},
        "peer_labels": {"tier": "frontend"},
        "port": 8443,
    },
    "rbac": {
        "sa_name": "deployer",
        "namespace": "prod",
        "role_name": "deployer-role",
        "role_kind": "Role",
        "resources": ["deployments", "pods"],
        "verbs": ["get", "list", "create"],
    },
    "pvc": {
        "name": "data",
        "namespace": "db",
        "access_mode": "ReadWriteOnce",
        "size": "100Mi",
        "storage_class": "slow",
    },
    "scheduling": {
        "name": "special",
        "namespace": "infra",
        "image": "redis:7.2",
        "replicas": 2,
        "labels": {"app": "special"},
        "node_label_key": "dedicated",
        "node_label_value": "gpu",
        "cpu_request": "200m",
        "memory_request": "256Mi",
    },
    "troubleshooting_crashloop": {
        "name": "broken",
        "namespace": "ops",
        "image": "nginx:1.27",
        "labels": {"app": "broken"},
        "replicas": 2,
        "failure": "bad_liveness",
    },
    "configmap_secret": {
        "name": "cfg",
        "namespace": "app",
        "cm_data": {"mode": "prod"},
        "secret_data": {"token": "s3cr3t"},
        "deploy_name": "app",
        "image": "nginx:1.27",
        "replicas": 2,
        "labels": {"app": "app"},
    },
    "fix_served_file": {
        "name": "web",
        "namespace": "app",
        "labels": {"app": "web"},
        "port": 80,
        "expected_content": "version 2.0",
        "bad_content": "version 1.0",
    },
    "cni_config": {
        "name": "cni",
        "namespace": "net",
        "key": "cni-config",
        "plugin_type": "calico",
        "cni_version": "1.0.0",
    },
    "autoscaling": {
        "name": "web-hpa",
        "namespace": "app",
        "workload": "web",
        "min": 1,
        "max": 5,
        "cpu_target": 50,
        "image": "nginx:1.27",
        "replicas": 1,
        "labels": {"app": "web"},
    },
    "helm": {
        "release": "web",
        "namespace": "app",
        "chart_name": "myapp",
        "image": "nginx:1.27",
        "replicas": 2,
        "service_port": 80,
    },
    "kustomize": {
        "namespace": "prod",
        "overlay": "prod",
        "name_prefix": "prod-",
        "base_name": "app",
        "image": "nginx:1.27",
        "replicas": 2,
    },
}


def _spec(archetype_id: str, params: dict | None = None) -> QuestionSpec:
    return QuestionSpec(archetype_id=archetype_id, params=params or VALID_PARAMS[archetype_id])


def test_every_registered_archetype_has_a_renderer():
    assert set(RENDERERS) == set(REGISTRY)


def test_new_archetypes_produce_files_and_assertions():
    cases = {
        "configmap_secret": {
            "name": "cfg",
            "namespace": "app",
            "cm_data": {"mode": "prod"},
            "secret_data": {"token": "s3cr3t"},
            "deploy_name": "app",
            "image": "nginx:1.27",
            "replicas": 2,
            "labels": {"app": "app"},
        },
        "fix_served_file": {
            "name": "web",
            "namespace": "app",
            "labels": {"app": "web"},
            "port": 80,
            "expected_content": "version 2.0",
            "bad_content": "version 1.0",
        },
        "cni_config": {
            "name": "cni",
            "namespace": "net",
            "key": "cni-config",
            "plugin_type": "calico",
            "cni_version": "1.0.0",
        },
        "autoscaling": {
            "name": "web-hpa",
            "namespace": "app",
            "workload": "web",
            "min": 1,
            "max": 5,
            "cpu_target": 50,
            "image": "nginx:1.27",
            "replicas": 1,
            "labels": {"app": "web"},
        },
        "helm": {
            "release": "web",
            "namespace": "app",
            "chart_name": "myapp",
            "image": "nginx:1.27",
            "replicas": 2,
            "service_port": 80,
        },
        "kustomize": {
            "namespace": "prod",
            "overlay": "prod",
            "name_prefix": "prod-",
            "base_name": "app",
            "image": "nginx:1.27",
            "replicas": 2,
        },
    }
    for archetype_id, params in cases.items():
        result = RENDERERS[archetype_id](_spec(archetype_id, params))
        assert result.task, archetype_id
        assert result.assertions, archetype_id
        assert result.reference_manifests, archetype_id
        if archetype_id in ("cni_config", "helm", "kustomize"):
            assert result.files, archetype_id


def test_fix_served_file_uses_content_assertion():
    result = RENDERERS["fix_served_file"](_spec("fix_served_file", {
        "name": "web",
        "namespace": "app",
        "labels": {"app": "web"},
        "port": 80,
        "expected_content": "version 2.0",
        "bad_content": "version 1.0",
    }))
    from cka_mock.assertion import ExecContentAssertion

    assert any(isinstance(a, ExecContentAssertion) for a in result.assertions)
    # reference is the corrected ConfigMap; setup mounts the broken one
    assert result.reference_manifests[0]["data"]["index.html"] == "version 2.0"
    assert result.setup_manifests[1]["data"]["index.html"] == "version 1.0"


def test_helm_chart_files_render_templates():
    result = RENDERERS["helm"](_spec("helm", {
        "release": "web",
        "namespace": "app",
        "chart_name": "myapp",
        "image": "nginx:1.27",
        "replicas": 2,
        "service_port": 80,
    }))
    by_path = {f.path: f.content for f in result.files}
    assert "helm/myapp/Chart.yaml" in by_path
    assert "helm/myapp/templates/deployment.yaml" in by_path
    assert "{{ .Release.Name }}" in by_path["helm/myapp/templates/deployment.yaml"]
    assert "{{ .Values.image }}" in by_path["helm/myapp/templates/deployment.yaml"]


def test_kustomize_reference_namespace_scoped():
    result = RENDERERS["kustomize"](_spec("kustomize", {
        "namespace": "prod",
        "overlay": "prod",
        "name_prefix": "prod-",
        "base_name": "app",
        "image": "nginx:1.27",
        "replicas": 2,
    }))
    reference = result.reference_manifests[0]
    assert reference["metadata"]["name"] == "prod-app"
    assert reference["metadata"]["namespace"] == "prod"
    assert any("Namespace" in {d.get("kind")} for d in result.setup_manifests)


def test_every_renderer_produces_artifacts():
    for archetype_id in RENDERERS:
        result = RENDERERS[archetype_id](_spec(archetype_id))
        assert result.task, archetype_id
        assert result.assertions, archetype_id
        assert result.setup_manifests, archetype_id
        assert result.reference_manifests, archetype_id
        # setup must create the namespace
        kinds = {doc.get("kind") for doc in result.setup_manifests}
        assert "Namespace" in kinds, archetype_id


def test_scheduling_renderer_labels_node():
    result = RENDERERS["scheduling"](_spec("scheduling"))
    assert result.node_required is True
    assert any("{{NODE}}" in command and "label" in command[0] for command in result.setup_commands)
    reference = result.reference_manifests[0]
    assert reference["spec"]["template"]["spec"]["nodeSelector"] == {"dedicated": "gpu"}


def test_networkpolicy_renders_probes():
    result = RENDERERS["networkpolicy"](_spec("networkpolicy"))
    exec_commands = [a.command for a in result.assertions if type(a).__name__ == "ExecAssertion"]
    assert len(exec_commands) == 2
    assert any("wget" in (cmd or []) for cmd in exec_commands)


def test_troubleshooting_renders_broken_and_fixed():
    result = RENDERERS["troubleshooting_crashloop"](_spec("troubleshooting_crashloop"))
    broken = result.setup_manifests[-1]
    assert broken["spec"]["template"]["spec"]["containers"][0].get("livenessProbe") is not None
    fixed = result.reference_manifests[-1]
    fixed_probe = fixed["spec"]["template"]["spec"]["containers"][0].get("livenessProbe")
    assert fixed_probe is None or fixed_probe["httpGet"]["path"] != "/health"


def test_render_exam_indexes_and_markdown():
    plan = ExamPlan(questions=[_spec("deployment"), _spec("service")])
    results = render_exam(plan)
    assert [r.question_index for r in results] == [1, 2]
    markdown = render_task_markdown(results)
    assert "### Q1" in markdown
    assert "### Q2" in markdown
    assert "Create a Deployment" in markdown
