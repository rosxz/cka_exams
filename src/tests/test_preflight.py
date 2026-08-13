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
