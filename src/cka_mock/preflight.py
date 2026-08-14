"""Preflight: prove each challenge is (a) unsolved on the pristine cluster and
(b) solvable — the reference solution satisfies every assertion. The reference
solution is applied, verified, then rolled back to the broken state, so the user
always starts from a genuine, winnable scenario.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .assertion import run_assertions
from .kubectl import Kubectl
from .renderer import RenderResult
from .setup import (
    apply_install_commands,
    apply_reference,
    apply_result_setup,
    apply_teardown_commands,
    wait_for_manifests,
    _dump,
)


class PreflightError(RuntimeError):
    pass


@dataclass
class PreflightReport:
    warnings: list[str] = field(default_factory=list)


def _overlaps_setup(result: RenderResult) -> bool:
    setup_keys = {(doc.get("kind"), (doc.get("metadata") or {}).get("name")) for doc in result.setup_manifests}
    return any(
        (doc.get("kind"), (doc.get("metadata") or {}).get("name")) in setup_keys
        for doc in result.reference_manifests
    )


def _delete_reference(kubectl: Kubectl, result: RenderResult) -> None:
    for doc in result.reference_manifests:
        kubectl.run(["delete", "-f", "-", "--ignore-not-found"], input=_dump(doc), timeout=120)


def _restore(
    kubectl: Kubectl, result: RenderResult, node_name: str, files_dir: Path | None
) -> None:
    if result.reference_teardown_commands:
        # Install-yourself archetype: remove the controller/CRDs the candidate
        # will install, leaving a clean cluster.
        apply_teardown_commands(kubectl, result, node_name, files_dir)
    elif _overlaps_setup(result):
        # Fix archetype: the reference replaced the broken object; re-apply the
        # broken setup so the user gets the broken state back.
        apply_result_setup(kubectl, result, node_name, files_dir)
    else:
        _delete_reference(kubectl, result)


def preflight_result(
    kubectl: Kubectl,
    result: RenderResult,
    node_name: str,
    *,
    files_dir: Path | None = None,
    ready_timeout: int = 360,
    satisfied_timeout: int = 120,
    unsolved_timeout: int = 90,
) -> PreflightReport:
    report = PreflightReport()

    initial = run_assertions(result.assertions, kubectl.run)
    if all(r.passed for r in initial):
        raise PreflightError(
            f"Q{result.question_index}: challenge is already solved before the user acts "
            f"(archetype {result.archetype_id}). Regenerate the exam."
        )

    try:
        if result.reference_install_commands:
            apply_install_commands(kubectl, result, node_name, files_dir)
            if result.reference_ready_assertions and not _wait_ready(
                kubectl, result.reference_ready_assertions, timeout=ready_timeout
            ):
                raise PreflightError(
                    f"Q{result.question_index}: controller install did not become ready "
                    f"(archetype {result.archetype_id})."
                )
        apply_reference(kubectl, result, node_name, files_dir)
        wait_for_manifests(kubectl, result.reference_manifests, timeout=240)
        # Give controllers (e.g. ingress-nginx route sync) a moment to observe
        # the reference before the satisfied-poll starts.
        time.sleep(4)
        if not _wait_satisfied(kubectl, result, timeout=satisfied_timeout):
            failed = [
                r.description for r in run_assertions(result.assertions, kubectl.run) if not r.passed
            ]
            raise PreflightError(
                f"Q{result.question_index}: reference solution does not satisfy the "
                f"challenge (archetype {result.archetype_id}). Failing checks: {failed}"
            )
    finally:
        _restore(kubectl, result, node_name, files_dir)

    if not _wait_unsolved(kubectl, result, timeout=unsolved_timeout):
        raise PreflightError(
            f"Q{result.question_index}: cluster still appears solved after restoring the "
            f"broken state (archetype {result.archetype_id}). Regenerate the exam."
        )

    return report


def _wait_ready(kubectl: Kubectl, assertions, timeout: int) -> bool:
    """Poll until the controller install readiness assertions all pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(r.passed for r in run_assertions(assertions, kubectl.run)):
            return True
        time.sleep(5)
    return False


def _wait_unsolved(kubectl: Kubectl, result: RenderResult, timeout: int) -> bool:
    """Poll until the restored broken state no longer satisfies the assertions."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not all(r.passed for r in run_assertions(result.assertions, kubectl.run)):
            return True
        time.sleep(3)
    return False


def _wait_satisfied(kubectl: Kubectl, result: RenderResult, timeout: int) -> bool:
    """Poll until every assertion passes (configmap propagation, probe warmup...)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = run_assertions(result.assertions, kubectl.run)
        if all(r.passed for r in current):
            return True
        if os.environ.get("CKA_MOCK_DEBUG"):
            failing = [r for r in current if not r.passed]
            detail = [
                res.description + " actual=" + repr(res.actual)
                for res in failing[:4]
            ]
            print(
                f"[debug] Q{result.question_index} ({result.archetype_id}) not satisfied: {detail}"
            )
            if any(res.description.startswith("endpoints") for res in failing):
                probe = kubectl.run(["get", "endpoints", "-n", "frontend", "-o", "json"])
                print("[debug] endpoints object:", probe.stdout[:500])
        time.sleep(3)
    return False
