from __future__ import annotations

import shutil

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
