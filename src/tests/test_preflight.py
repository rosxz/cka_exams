from __future__ import annotations

import subprocess

import pytest

from cka_mock.kubectl import Kubectl
from cka_mock.preflight import PreflightError, preflight_result
from cka_mock.renderer import RENDERERS
from cka_mock.schemas import QuestionSpec


def _deploy_spec():
    return QuestionSpec(
        archetype_id="deployment",
        params={
            "name": "web",
            "namespace": "frontend",
            "image": "nginx:1.27",
            "replicas": 3,
            "labels": {"app": "web"},
            "container_port": 80,
        },
    )


def _scripted_environment(fake_tooling, *, deployed: bool, image: str = "nginx:1.27"):
    """A tiny in-memory 'cluster' for the deployment archetype."""
    state = {"deployed": deployed}
    jsonpath_values = {
        ".spec.replicas": "3",
        ".spec.template.spec.containers[0].image": image,
        ".spec.template.metadata.labels": '{"app":"web"}',
        ".status.availableReplicas": "3",
    }

    @fake_tooling.when("kubectl", "apply")
    def apply(argv):
        state["deployed"] = True
        return subprocess.CompletedProcess(argv, 0, "applied", "")

    @fake_tooling.when("kubectl", "delete")
    def delete(argv):
        state["deployed"] = False
        return subprocess.CompletedProcess(argv, 0, "deleted", "")

    @fake_tooling.when("kubectl", "rollout")
    def rollout(argv):
        return subprocess.CompletedProcess(argv, 0, "successfully rolled out", "")

    @fake_tooling.when("kubectl", "get", "deployments.apps")
    def get(argv):
        if not state["deployed"]:
            return subprocess.CompletedProcess(argv, 1, "", "NotFound")
        try:
            oi = argv.index("-o")
            path = argv[oi + 1].split("jsonpath=", 1)[1].strip().strip("{}")
        except ValueError:
            return subprocess.CompletedProcess(argv, 0, "web", "")
        return subprocess.CompletedProcess(argv, 0, jsonpath_values.get(path, "0"), "")

    return state


def test_preflight_passes_and_restores(fake_tooling):
    state = _scripted_environment(fake_tooling, deployed=False)
    result = RENDERERS["deployment"](_deploy_spec())
    report = preflight_result(Kubectl(), result, node_name="minikube")
    assert not report.warnings
    assert state["deployed"] is False  # reference rolled back


def test_preflight_rejects_already_solved(fake_tooling):
    _scripted_environment(fake_tooling, deployed=True)
    result = RENDERERS["deployment"](_deploy_spec())
    with pytest.raises(PreflightError, match="already solved"):
        preflight_result(Kubectl(), result, node_name="minikube")


def test_preflight_rejects_unsolvable_reference(fake_tooling):
    # The reference "applies" but the cluster's image never matches expectation.
    _scripted_environment(fake_tooling, deployed=False, image="busybox:1.36")
    result = RENDERERS["deployment"](_deploy_spec())
    with pytest.raises(PreflightError, match="reference solution"):
        preflight_result(Kubectl(), result, node_name="minikube", satisfied_timeout=6)


def test_preflight_restores_fix_archetype(fake_tooling):
    """Fix archetypes (overlapping reference) must re-apply the broken state."""
    from cka_mock.renderer import _dump

    applied = []

    @fake_tooling.when("kubectl", "apply")
    def apply(argv):
        applied.append(argv)
        return subprocess.CompletedProcess(argv, 0, "applied", "")

    @fake_tooling.when("kubectl", "rollout")
    def rollout(argv):
        return subprocess.CompletedProcess(argv, 0, "successfully rolled out", "")

    @fake_tooling.when("kubectl", "get", "pods")
    def get_pods(argv):
        return subprocess.CompletedProcess(argv, 0, "", "")

    # A broken Deployment that is crash-looping never reports available replicas.
    @fake_tooling.when("kubectl", "get", "deployments.apps")
    def get(argv):
        return subprocess.CompletedProcess(argv, 1, "", "not found")

    spec = QuestionSpec(
        archetype_id="troubleshooting_crashloop",
        params={
            "name": "broken",
            "namespace": "ops",
            "image": "nginx:1.27",
            "labels": {"app": "broken"},
            "replicas": 2,
            "failure": "exit_immediately",
        },
    )
    result = RENDERERS["troubleshooting_crashloop"](spec)
    with pytest.raises(PreflightError, match="reference solution"):
        preflight_result(Kubectl(), result, node_name="minikube", satisfied_timeout=6)


def test_preflight_install_yourself_flow(fake_tooling, tmp_path):
    """Install-yourself archetypes: install -> ready -> reference -> teardown."""
    state = {"solved": False}
    gateway_host = "gw.example.com"
    svc_name = "web-route-backend-svc"

    @fake_tooling.when("kubectl", "apply")
    def apply(argv):
        # reference manifests come via stdin; install commands reference a file path
        if "-" in argv:
            state["solved"] = True
        return subprocess.CompletedProcess(argv, 0, "applied", "")

    @fake_tooling.when("kubectl", "delete")
    def delete(argv):
        state["solved"] = False
        return subprocess.CompletedProcess(argv, 0, "deleted", "")

    @fake_tooling.when("kubectl", "get", "deployments.apps")
    def get_deploy(argv):
        return subprocess.CompletedProcess(argv, 0, "1", "")

    @fake_tooling.when("kubectl", "get", "gatewayclasses.gateway.networking.k8s.io")
    def get_gc(argv):
        if not state["solved"]:
            return subprocess.CompletedProcess(argv, 1, "", "NotFound")
        if "-o" in argv:
            path = argv[argv.index("-o") + 1].split("jsonpath=", 1)[1]
            value = "projectcontour.io/gateway-controller" if "controllerName" in path else ""
            return subprocess.CompletedProcess(argv, 0, value, "")
        return subprocess.CompletedProcess(argv, 0, "contour", "")

    @fake_tooling.when("kubectl", "get", "gateways.gateway.networking.k8s.io")
    def get_gw(argv):
        if not state["solved"]:
            return subprocess.CompletedProcess(argv, 1, "", "NotFound")
        if "-o" in argv:
            path = argv[argv.index("-o") + 1].split("jsonpath=", 1)[1]
            return subprocess.CompletedProcess(argv, 0, "True" if "status" in path else "", "")
        return subprocess.CompletedProcess(argv, 0, "contour", "")

    @fake_tooling.when("kubectl", "get", "httproutes.gateway.networking.k8s.io")
    def get_hr(argv):
        if not state["solved"]:
            return subprocess.CompletedProcess(argv, 1, "", "NotFound")
        if "-o" in argv:
            path = argv[argv.index("-o") + 1].split("jsonpath=", 1)[1]
            value = {
                "{.spec.hostnames[0]}": gateway_host,
                "{.spec.rules[0].backendRefs[0].name}": svc_name,
                "{.spec.rules[0].backendRefs[0].port}": "80",
                "{.spec.parentRefs[0].name}": "contour",
            }.get(path, "")
            return subprocess.CompletedProcess(argv, 0, value, "")
        return subprocess.CompletedProcess(argv, 0, "web-route", "")

    @fake_tooling.when("kubectl", "get", "customresourcedefinitions.apiextensions.k8s.io")
    def get_crd(argv):
        if state["solved"]:
            return subprocess.CompletedProcess(argv, 0, "crd", "")
        return subprocess.CompletedProcess(argv, 1, "", "NotFound")

    result = RENDERERS["gateway"](QuestionSpec("gateway", {
        "name": "web-route", "namespace": "gwapp", "host": gateway_host,
        "labels": {"app": "web"}, "port": 80, "replicas": 1,
    }))
    report = preflight_result(Kubectl(), result, node_name="minikube", files_dir=tmp_path)
    assert not report.warnings
    # teardown left the cluster unsolved
    assert state["solved"] is False
    delete_calls = [c for c in fake_tooling.calls if c[:2] == ["kubectl", "delete"]]
    assert delete_calls, "teardown commands must run"
    install_calls = [c for c in fake_tooling.calls if c[:2] == ["kubectl", "apply"]]
    assert any(c[2] == "-f" and c[3] != "-" for c in install_calls), "install commands must run"
