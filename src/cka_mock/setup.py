"""Applying exam setup and waiting for readiness.

Everything applied here comes from the deterministic renderer. The LLM never
reaches this path. Node tokens (``{{NODE}}``) are substituted with the real node
name and file tokens (``{{FILES}}``) with the exam workdir files directory before
any command runs.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .kubectl import Kubectl
from .renderer import FILES_TOKEN, NODE_TOKEN, RenderResult, _dump


def _substitute(
    argv: list[str], node_name: str, files_dir: str | Path | None = None
) -> list[str]:
    substituted = [arg.replace(NODE_TOKEN, node_name) for arg in argv]
    if files_dir is not None:
        substituted = [arg.replace(FILES_TOKEN, str(files_dir)) for arg in substituted]
    return substituted


def apply_manifest(
    kubectl: Kubectl, doc: dict, timeout: int = 120, attempts: int = 6
) -> subprocess.CompletedProcess:
    """Apply a manifest, retrying on transient failures.

    Retries cover short-lived API-server hiccups and races such as creating a
    custom resource immediately after its CRD (which must first be established).
    """
    last: subprocess.CompletedProcess | None = None
    for _ in range(attempts):
        last = kubectl.run(["apply", "-f", "-"], input=_dump(doc), timeout=timeout)
        if last.returncode == 0:
            return last
        time.sleep(4)
    return last


def _apply_with_check(kubectl: Kubectl, doc: dict, what: str) -> None:
    proc = apply_manifest(kubectl, doc)
    if proc.returncode != 0:
        raise RuntimeError(f"{what} apply failed for {doc.get('kind')}: {proc.stderr.strip()}")


def apply_result_setup(
    kubectl: Kubectl, result: RenderResult, node_name: str, files_dir: str | Path | None = None
) -> None:
    for doc in result.setup_manifests:
        _apply_with_check(kubectl, doc, "setup")
    for command in result.setup_commands:
        kubectl.run(_substitute(command, node_name, files_dir), timeout=120)


def apply_reference(
    kubectl: Kubectl, result: RenderResult, node_name: str, files_dir: str | Path | None = None
) -> None:
    for doc in result.reference_manifests:
        _apply_with_check(kubectl, doc, "reference")
    for command in result.reference_commands:
        kubectl.run(_substitute(command, node_name, files_dir), timeout=120)
    if result.reference_shell_commands:
        _run_shell_commands(kubectl, result.reference_shell_commands, files_dir)


def _run_shell_commands(
    kubectl: Kubectl, commands: list[str], files_dir: str | Path | None
) -> None:
    """Run raw shell commands with the exam KUBECONFIG exported.

    Used when the reference must capture kubectl stdout into an artifact (e.g.
    decoding a signed certificate, or seeding a ConfigMap with a query's output)
    which cannot be expressed as static reference manifests. Everything here is
    authored by the renderer, never by the LLM.
    """
    env = os.environ.copy()
    if kubectl.kubeconfig:
        env["KUBECONFIG"] = kubectl.kubeconfig
    if files_dir is not None:
        env["FILES_DIR"] = str(files_dir)
    for command in commands:
        substituted = subprocess.run(
            ["sh", "-c", command.replace(FILES_TOKEN, str(files_dir))],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        if substituted.returncode != 0:
            raise RuntimeError(
                f"reference shell command failed: {command}: {substituted.stderr.strip()}"
            )


def apply_install_commands(
    kubectl: Kubectl, result: RenderResult, node_name: str, files_dir: str | Path | None = None
) -> None:
    """Run the commands that install cluster-scoped components (CRDs, controller).

    Retried because install manifests often contain resources that depend on
    CRDs defined earlier in the same file (the first pass establishes them).
    """
    for command in result.reference_install_commands:
        last: subprocess.CompletedProcess | None = None
        for _ in range(3):
            last = kubectl.run(_substitute(command, node_name, files_dir), timeout=600)
            if last.returncode == 0:
                break
            time.sleep(5)
        if last is not None and last.returncode != 0:
            raise RuntimeError(f"install command failed: {command}: {last.stderr.strip()}")


def apply_teardown_commands(
    kubectl: Kubectl, result: RenderResult, node_name: str, files_dir: str | Path | None = None
) -> None:
    """Run the commands that fully remove an install-yourself challenge's install."""
    for command in result.reference_teardown_commands:
        kubectl.run(_substitute(command, node_name, files_dir), timeout=300)


def _wait_one(kubectl: Kubectl, kind: str, name: str, namespace: str | None, timeout: int) -> bool:
    ns_args = ["-n", namespace] if namespace else []
    if kind == "Deployment":
        argv = ["rollout", "status", f"deployment/{name}"] + ns_args + ["--timeout", f"{timeout}s"]
    elif kind == "Pod":
        argv = ["wait", "--for=condition=Ready", f"pod/{name}"] + ns_args + ["--timeout", f"{timeout}s"]
    elif kind == "PersistentVolumeClaim":
        argv = ["wait", "--for=jsonpath={.status.phase}=Bound", f"pvc/{name}"] + ns_args + ["--timeout", f"{timeout}s"]
    else:
        return True
    try:
        proc = kubectl.run(argv, timeout=timeout + 30)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def wait_for_manifests(
    kubectl: Kubectl,
    manifests: list[dict],
    *,
    skip_kinds: set[str] | None = None,
    skip_objects: set[tuple[str, str]] | None = None,
    timeout: int = 240,
) -> list[str]:
    """Wait for deployments/pods/pvcs to be ready. Returns warnings for anything
    that did not reach readiness in time (an expected state, e.g. a crash-looping
    or unschedulable setup Deployment, is reported as a warning, not an error)."""
    warnings: list[str] = []
    skip_kinds = skip_kinds or set()
    skip_objects = skip_objects or set()
    for doc in manifests:
        kind = doc.get("kind")
        metadata = doc.get("metadata") or {}
        name = metadata.get("name")
        namespace = metadata.get("namespace")
        if kind in skip_kinds or not name:
            continue
        if (kind, name) in skip_objects:
            continue
        if not _wait_one(kubectl, kind, name, namespace, timeout):
            warnings.append(f"{kind.lower()}/{name} did not become ready in time")
    return warnings
