from __future__ import annotations

import shutil

import pytest

from cka_mock.env import MinikubeEnv


def _fake_bins(monkeypatch):
    monkeypatch.setattr(
        shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in ("minikube", "kubectl") else None,
    )


def test_export_kubeconfig(fake_tooling, tmp_path, monkeypatch):
    _fake_bins(monkeypatch)
    fake_tooling.respond(
        "kubectl", "config", "view", "--context", "cka-exam", "--minify", "--raw",
        stdout="apiVersion: v1\nclusters: []\n",
    )
    env = MinikubeEnv(profile="cka-exam")
    out = tmp_path / "kc" / "kubeconfig"
    written = env.export_kubeconfig(out)
    assert written.read_text().startswith("apiVersion: v1")


def test_node_name(fake_tooling, monkeypatch):
    _fake_bins(monkeypatch)
    fake_tooling.respond(
        "kubectl", "--context", "cka-exam", "get", "nodes",
        "-o", "jsonpath={.items[0].metadata.name}", stdout="minikube",
    )
    env = MinikubeEnv(profile="cka-exam")
    assert env.node_name() == "minikube"


def test_delete_uses_own_profile_only(fake_tooling, monkeypatch):
    _fake_bins(monkeypatch)
    fake_tooling.respond("minikube", "-p", "cka-exam", "delete")
    env = MinikubeEnv(profile="cka-exam")
    env.delete()
    assert any(call[:4] == ["minikube", "-p", "cka-exam", "delete"] for call in fake_tooling.calls)
    assert not any("--all" in call for call in fake_tooling.calls)


def test_kubectl_context_is_pinned(fake_tooling):
    env = MinikubeEnv(profile="cka-exam")
    kubectl = env.kubectl()
    assert kubectl.context == "cka-exam"


def test_enable_ingress_labels_nodes_and_waits_ready(fake_tooling, monkeypatch):
    _fake_bins(monkeypatch)
    for label in ("minikube.k8s.io/primary=true", "ingress-ready=true"):
        fake_tooling.respond(
            "kubectl", "--context", "cka-exam", "label", "nodes", "--all", label, "--overwrite"
        )
    fake_tooling.respond("minikube", "-p", "cka-exam", "addons", "enable", "ingress")
    fake_tooling.respond(
        "kubectl", "--context", "cka-exam", "get", "deploy", "ingress-nginx-controller",
        "-n", "ingress-nginx", "-o", "jsonpath={.status.availableReplicas}", stdout="1",
    )
    for job in ("ingress-nginx-admission-create", "ingress-nginx-admission-patch"):
        fake_tooling.respond(
            "kubectl", "--context", "cka-exam", "get", "job", job,
            "-n", "ingress-nginx", "-o", "jsonpath={.status.succeeded}", stdout="1",
        )
    fake_tooling.respond("kubectl", "--context", "cka-exam", "apply", "-f", "-")
    fake_tooling.respond(
        "kubectl", "--context", "cka-exam", "delete", "ingress",
        "cka-webhook-probe", "-n", "ingress-nginx", "--ignore-not-found",
    )

    env = MinikubeEnv(profile="cka-exam", addons=("ingress",))
    env._enable_addons(log=lambda _m: None)

    label_calls = [call for call in fake_tooling.calls if "label" in call]
    assert any("minikube.k8s.io/primary=true" in call for call in label_calls)
    assert any("ingress-ready=true" in call for call in label_calls)
    assert any("apply" in call for call in fake_tooling.calls)  # webhook probe ran


def test_enable_ingress_retries_webhook_probe(fake_tooling, monkeypatch):
    import subprocess

    _fake_bins(monkeypatch)
    for label in ("minikube.k8s.io/primary=true", "ingress-ready=true"):
        fake_tooling.respond(
            "kubectl", "--context", "cka-exam", "label", "nodes", "--all", label, "--overwrite"
        )
    fake_tooling.respond("minikube", "-p", "cka-exam", "addons", "enable", "ingress")
    fake_tooling.respond(
        "kubectl", "--context", "cka-exam", "get", "deploy", "ingress-nginx-controller",
        "-n", "ingress-nginx", "-o", "jsonpath={.status.availableReplicas}", stdout="1",
    )
    for job in ("ingress-nginx-admission-create", "ingress-nginx-admission-patch"):
        fake_tooling.respond(
            "kubectl", "--context", "cka-exam", "get", "job", job,
            "-n", "ingress-nginx", "-o", "jsonpath={.status.succeeded}", stdout="1",
        )
    fake_tooling.respond(
        "kubectl", "--context", "cka-exam", "delete", "ingress",
        "cka-webhook-probe", "-n", "ingress-nginx", "--ignore-not-found",
    )

    apply_count = {"n": 0}

    @fake_tooling.when("kubectl", "--context", "cka-exam", "apply")
    def apply(argv):
        apply_count["n"] += 1
        if apply_count["n"] == 1:
            return subprocess.CompletedProcess(argv, 1, "", "webhook not ready yet")
        return subprocess.CompletedProcess(argv, 0, "created", "")

    env = MinikubeEnv(profile="cka-exam", addons=("ingress",))
    env._enable_addons(log=lambda _m: None)
    assert apply_count["n"] >= 2


def test_enable_ingress_raises_if_never_ready(fake_tooling, monkeypatch):
    import subprocess

    _fake_bins(monkeypatch)
    for label in ("minikube.k8s.io/primary=true", "ingress-ready=true"):
        fake_tooling.respond(
            "kubectl", "--context", "cka-exam", "label", "nodes", "--all", label, "--overwrite"
        )
    fake_tooling.respond("minikube", "-p", "cka-exam", "addons", "enable", "ingress")
    fake_tooling.respond(
        "kubectl", "--context", "cka-exam", "get", "deploy", "ingress-nginx-controller",
        "-n", "ingress-nginx", "-o", "jsonpath={.status.availableReplicas}", stdout="1",
    )
    for job in ("ingress-nginx-admission-create", "ingress-nginx-admission-patch"):
        fake_tooling.respond(
            "kubectl", "--context", "cka-exam", "get", "job", job,
            "-n", "ingress-nginx", "-o", "jsonpath={.status.succeeded}", stdout="1",
        )
    fake_tooling.respond(
        "kubectl", "--context", "cka-exam", "delete", "ingress",
        "cka-webhook-probe", "-n", "ingress-nginx", "--ignore-not-found",
    )

    @fake_tooling.when("kubectl", "--context", "cka-exam", "apply")
    def apply(argv):
        return subprocess.CompletedProcess(argv, 1, "", "webhook down")

    from cka_mock.env import MinikubeError

    env = MinikubeEnv(profile="cka-exam", addons=("ingress",))
    with pytest.raises(MinikubeError, match="ingress addon did not become ready"):
        env._enable_addons(log=lambda _m: None, ingress_timeout=6)
