"""Dedicated Minikube environment management.

Only one profile (``cka-exam`` by default) is ever touched. Every operation is
pinned to that profile; the user's other clusters and profiles are untouched.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .kubectl import Kubectl


class MinikubeError(RuntimeError):
    pass


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


@dataclass
class MinikubeEnv:
    profile: str = "cka-exam"
    driver: str | None = None
    cpus: int | None = None
    memory: int | None = None
    cni: str | None = "calico"
    addons: tuple[str, ...] = ("ingress", "metrics-server")

    def _base(self) -> list[str]:
        if not shutil.which("minikube"):
            raise MinikubeError(
                "minikube not found on PATH. Use `nix develop` or install minikube."
            )
        return ["minikube", "-p", self.profile]

    def kubectl(self, kubeconfig: Path | None = None) -> Kubectl:
        return Kubectl(context=self.profile, kubeconfig=kubeconfig)

    def start(self, *, reset: bool = True, wait_timeout: int = 600) -> None:
        if reset:
            self.delete()
        cmd = self._base() + ["start"]
        if self.driver:
            cmd += ["--driver", self.driver]
        if self.cpus:
            cmd += ["--cpus", str(self.cpus)]
        if self.memory:
            cmd += ["--memory", str(self.memory)]
        if self.cni:
            # The default minikube CNI does not enforce NetworkPolicies; use a
            # policy-capable CNI so NetworkPolicy challenges are verifiable.
            cmd += ["--network-plugin=cni", f"--cni={self.cni}"]
        proc = _run(cmd, timeout=900)
        if proc.returncode != 0:
            raise MinikubeError(f"minikube start failed: {proc.stderr.strip()}")
        self._wait_nodes_ready(wait_timeout)
        for addon in self.addons:
            addon_proc = _run(self._base() + ["addons", "enable", addon], timeout=300)
            if addon_proc.returncode != 0 and "already enabled" not in addon_proc.stderr:
                raise MinikubeError(
                    f"failed to enable addon {addon}: {addon_proc.stderr.strip()}"
                )

    def _wait_nodes_ready(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            proc = _run(
                [
                    "kubectl", "--context", self.profile, "get", "nodes",
                    "-o", "jsonpath={.items[0].status.conditions[?(@.type==\"Ready\")].status}",
                ],
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout.strip() == "True":
                return
            time.sleep(3)
        raise MinikubeError("minikube cluster did not become ready in time")

    def export_kubeconfig(self, path: Path) -> Path:
        proc = _run(
            ["kubectl", "config", "view", "--context", self.profile, "--minify", "--raw"],
            timeout=30,
        )
        if proc.returncode != 0:
            raise MinikubeError(f"could not export kubeconfig: {proc.stderr.strip()}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(proc.stdout)
        return path

    def node_name(self, kubectl: Kubectl | None = None) -> str:
        kubectl = kubectl or self.kubectl()
        proc = kubectl.run(["get", "nodes", "-o", "jsonpath={.items[0].metadata.name}"], timeout=30)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise MinikubeError("could not determine node name")
        return proc.stdout.strip()

    def status(self) -> dict:
        proc = _run(self._base() + ["status", "-o", "json"], timeout=60)
        if proc.returncode != 0:
            return {"running": False, "error": proc.stderr.strip()}
        try:
            status = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"running": False, "error": "unparseable minikube status"}
        apiserver = status.get("APIServer")
        return {"running": apiserver == "Running" or str(apiserver).lower() == "running",
                "profile": self.profile}

    def delete(self) -> None:
        _run(self._base() + ["delete"], timeout=300)
