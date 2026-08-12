"""Applying exam setup and waiting for readiness.

Everything applied here comes from the deterministic renderer. The LLM never
reaches this path. Node tokens (``{{NODE}}``) are substituted with the real node
name before any command runs.
"""
from __future__ import annotations

import subprocess
import time

from .kubectl import Kubectl
from .renderer import NODE_TOKEN, RenderResult, _dump


def _substitute(argv: list[str], node_name: str) -> list[str]:
    return [arg.replace(NODE_TOKEN, node_name) for arg in argv]


def apply_manifest(kubectl: Kubectl, doc: dict, timeout: int = 120) -> subprocess.CompletedProcess:
    return kubectl.run(["apply", "-f", "-"], input=_dump(doc), timeout=timeout)


def apply_result_setup(kubectl: Kubectl, result: RenderResult, node_name: str) -> None:
    for doc in result.setup_manifests:
        proc = apply_manifest(kubectl, doc)
        if proc.returncode != 0:
            raise RuntimeError(f"setup apply failed for {doc.get('kind')}: {proc.stderr.strip()}")
    for command in result.setup_commands:
        kubectl.run(_substitute(command, node_name), timeout=120)


def apply_reference(kubectl: Kubectl, result: RenderResult, node_name: str) -> None:
    for doc in result.reference_manifests:
        proc = apply_manifest(kubectl, doc)
        if proc.returncode != 0:
            raise RuntimeError(f"reference apply failed for {doc.get('kind')}: {proc.stderr.strip()}")
    for command in result.reference_commands:
        kubectl.run(_substitute(command, node_name), timeout=120)


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
    timeout: int = 240,
) -> list[str]:
    """Wait for deployments/pods/pvcs to be ready. Returns warnings for anything
    that did not reach readiness in time (an expected state, e.g. a crash-looping
    setup Deployment, is reported as a warning, not an error)."""
    warnings: list[str] = []
    skip_kinds = skip_kinds or set()
    for doc in manifests:
        kind = doc.get("kind")
        metadata = doc.get("metadata") or {}
        name = metadata.get("name")
        namespace = metadata.get("namespace")
        if kind in skip_kinds or not name:
            continue
        if not _wait_one(kubectl, kind, name, namespace, timeout):
            warnings.append(f"{kind.lower()}/{name} did not become ready in time")
    return warnings
