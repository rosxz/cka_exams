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


def test_start_reuses_running_compatible_cluster(fake_tooling, monkeypatch):
    _fake_bins(monkeypatch)
    fake_tooling.respond("minikube", "-p", "cka-exam", "status", "-o", "json",
                         stdout='{"APIServer":"Running","K8sVersion":"v1.35.1"}')
    fake_tooling.respond("minikube", "-p", "cka-exam", "addons", "list", "-o", "json",
                         stdout='{"metrics-server":{"Status":"enabled"},"ingress":{"Status":"enabled"}}')
    fake_tooling.respond("kubectl", "--context", "cka-exam", "get", "nodes",
                         "-o", "jsonpath={.items[0].status.conditions[?(@.type==\"Ready\")].status}",
                         stdout="True")
    # Reuse path: no minikube start/delete/addons-enable should run.
    env = MinikubeEnv(profile="cka-exam", addons=("metrics-server", "ingress"))
    env.start(reset=False, preload_images=False, log=lambda _m: None)
    assert not any("start" in call for call in fake_tooling.calls)
    assert not any("delete" in call for call in fake_tooling.calls)
    assert not any("addons" in call and "enable" in call for call in fake_tooling.calls)


def test_start_resets_when_requested(fake_tooling, monkeypatch):
    _fake_bins(monkeypatch)
    fake_tooling.respond("minikube", "-p", "cka-exam", "status", "-o", "json",
                         stdout='{"APIServer":"Not Running"}')
    fake_tooling.respond("minikube", "-p", "cka-exam", "delete")
    fake_tooling.respond("minikube", "-p", "cka-exam", "start", "--network-plugin=cni", "--cni=calico",
                         stdout="* Done!")
    fake_tooling.respond("minikube", "-p", "cka-exam", "addons", "list", "-o", "json",
                         stdout='{}')
    fake_tooling.respond("minikube", "-p", "cka-exam", "addons", "enable", "metrics-server")
    fake_tooling.respond("kubectl", "--context", "cka-exam", "get", "nodes",
                         "-o", "jsonpath={.items[0].status.conditions[?(@.type==\"Ready\")].status}",
                         stdout="True")
    env = MinikubeEnv(profile="cka-exam", addons=("metrics-server",))
    env.start(reset=True, preload_images=False, log=lambda _m: None)
    assert any("delete" in call for call in fake_tooling.calls)
    assert any("start" in call for call in fake_tooling.calls)


def test_start_does_not_reuse_when_addon_missing(fake_tooling, monkeypatch):
    _fake_bins(monkeypatch)
    fake_tooling.respond("minikube", "-p", "cka-exam", "status", "-o", "json",
                         stdout='{"APIServer":"Running","K8sVersion":"v1.35.1"}')
    fake_tooling.respond("minikube", "-p", "cka-exam", "addons", "list", "-o", "json",
                         stdout='{"metrics-server":{"Status":"disabled"}}')
    fake_tooling.respond("minikube", "-p", "cka-exam", "delete")
    fake_tooling.respond("minikube", "-p", "cka-exam", "start", "--network-plugin=cni", "--cni=calico",
                         stdout="* Done!")
    for label in ("minikube.k8s.io/primary=true", "ingress-ready=true"):
        fake_tooling.respond(
            "kubectl", "--context", "cka-exam", "label", "nodes", "--all", label, "--overwrite"
        )
    fake_tooling.respond("minikube", "-p", "cka-exam", "addons", "enable", "ingress")
    fake_tooling.respond("kubectl", "--context", "cka-exam", "get", "nodes",
                         "-o", "jsonpath={.items[0].status.conditions[?(@.type==\"Ready\")].status}",
                         stdout="True")
    fake_tooling.respond("kubectl", "--context", "cka-exam", "get", "deploy", "ingress-nginx-controller",
                         "-n", "ingress-nginx", "-o", "jsonpath={.status.availableReplicas}", stdout="1")
    for job in ("ingress-nginx-admission-create", "ingress-nginx-admission-patch"):
        fake_tooling.respond(
            "kubectl", "--context", "cka-exam", "get", "job", job,
            "-n", "ingress-nginx", "-o", "jsonpath={.status.succeeded}", stdout="1",
        )
    fake_tooling.respond("kubectl", "--context", "cka-exam", "delete", "ingress",
                         "cka-webhook-probe", "-n", "ingress-nginx", "--ignore-not-found")
    fake_tooling.respond("kubectl", "--context", "cka-exam", "apply", "-f", "-")
    env = MinikubeEnv(profile="cka-exam", addons=("ingress",))
    env.start(reset=False, preload_images=False, log=lambda _m: None)
    assert any("addons" in call and "enable" in call for call in fake_tooling.calls)


def test_preload_images_pulls_allowlist_in_parallel(fake_tooling, monkeypatch):
    _fake_bins(monkeypatch)
    fake_tooling.respond("minikube", "-p", "cka-exam", "image", "ls", stdout="registry.k8s.io/pause:3.10")

    @fake_tooling.when("minikube", "-p", "cka-exam", "image", "pull")
    def pull(argv):
        return subprocess.CompletedProcess(argv, 0, "", "")

    env = MinikubeEnv(profile="cka-exam", addons=("metrics-server",))
    env._preload_images(log=lambda _m: None)
    pulls = [call for call in fake_tooling.calls if "pull" in call]
    pulled_images = {call[-1] for call in pulls}
    assert "nginx:1.27" in pulled_images
    assert "redis:7.2" in pulled_images
    assert len(pulls) >= 4  # several allowlisted images preloaded in parallel


def test_preload_images_skips_present_images(fake_tooling, monkeypatch):
    _fake_bins(monkeypatch)
    from cka_mock.schemas import IMAGE_ALLOWLIST
    present = "registry.k8s.io/pause:3.10\n" + "\n".join(
        "docker.io/library/" + img for img in IMAGE_ALLOWLIST
    ) + "\nquay.io/calico/node:v3.31.3\nquay.io/calico/cni:v3.31.3"
    fake_tooling.respond("minikube", "-p", "cka-exam", "image", "ls", stdout=present)
    env = MinikubeEnv(profile="cka-exam", addons=("metrics-server",))
    env._preload_images(log=lambda _m: None)
    assert not any("pull" in call for call in fake_tooling.calls)
