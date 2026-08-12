#!/usr/bin/env python3
"""End-to-end smoke test: full loop against a real minikube cluster.

Replicates `cka-mock new` (env start, setup, preflight) with a static LLM
response covering every archetype, then "solves" each question by applying the
reference solution, waits until every check passes, and asserts a 100% grade.

Run from inside `nix develop`:

    python scripts/e2e.py

This is destructive: it deletes and recreates the `cka-exam` minikube profile.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from cka_mock.env import MinikubeEnv
from cka_mock.generation import generate_exam_plan
from cka_mock.grader import grade_exam
from cka_mock.preflight import PreflightError, _wait_satisfied, preflight_result
from cka_mock.providers import StaticProvider
from cka_mock.renderer import render_exam
from cka_mock.setup import apply_reference, apply_result_setup, wait_for_manifests
from cka_mock.workdir import Workdir

PAYLOAD = {
    "questions": [
        {
            "archetype": "deployment",
            "params": {
                "name": "web",
                "namespace": "frontend",
                "image": "nginx:1.27",
                "replicas": 2,
                "labels": {"app": "web"},
                "container_port": 80,
            },
        },
        {
            "archetype": "service",
            "params": {
                "name": "web-svc",
                "namespace": "frontend",
                "service_type": "NodePort",
                "port": 80,
                "target_port": 8080,
                "backend_labels": {"app": "web"},
                "backend_image": "nginx:1.27",
                "backend_replicas": 1,
            },
        },
        {
            "archetype": "pvc",
            "params": {
                "name": "data",
                "namespace": "db",
                "access_mode": "ReadWriteOnce",
                "size": "100Mi",
                "storage_class": "slow",
            },
        },
        {
            "archetype": "networkpolicy",
            "params": {
                "name": "allow-api",
                "namespace": "api",
                "target_labels": {"app": "api"},
                "peer_labels": {"tier": "frontend"},
                "port": 8000,
            },
        },
        {
            "archetype": "rbac",
            "params": {
                "sa_name": "deployer",
                "namespace": "prod",
                "role_name": "deployer-role",
                "role_kind": "Role",
                "resources": ["deployments", "pods"],
                "verbs": ["get", "list", "create"],
            },
        },
        {
            "archetype": "scheduling",
            "params": {
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
        },
        {
            "archetype": "troubleshooting_crashloop",
            "params": {
                "name": "broken",
                "namespace": "ops",
                "image": "nginx:1.27",
                "labels": {"app": "broken"},
                "replicas": 2,
                "failure": "bad_liveness",
            },
        },
        {
            "archetype": "configmap_secret",
            "params": {
                "name": "cfg",
                "namespace": "cfgapp",
                "cm_data": {"mode": "prod", "region": "eu"},
                "secret_data": {"token": "s3cr3t"},
                "deploy_name": "cfg-app",
                "image": "nginx:1.27",
                "replicas": 1,
                "labels": {"app": "cfg-app"},
            },
        },
        {
            "archetype": "fix_served_file",
            "params": {
                "name": "web",
                "namespace": "files",
                "labels": {"app": "web"},
                "port": 80,
                "expected_content": "version 2.0",
                "bad_content": "version 1.0",
            },
        },
        {
            "archetype": "cni_config",
            "params": {
                "name": "cni",
                "namespace": "net",
                "key": "cni-config",
                "plugin_type": "calico",
                "cni_version": "1.0.0",
            },
        },
        {
            "archetype": "autoscaling",
            "params": {
                "name": "web-hpa",
                "namespace": "hpa",
                "workload": "web",
                "min": 1,
                "max": 5,
                "cpu_target": 50,
                "image": "nginx:1.27",
                "replicas": 1,
                "labels": {"app": "web"},
            },
        },
        {
            "archetype": "helm",
            "params": {
                "release": "web",
                "namespace": "helmapp",
                "chart_name": "myapp",
                "image": "nginx:1.27",
                "replicas": 2,
                "service_port": 80,
            },
        },
        {
            "archetype": "kustomize",
            "params": {
                "namespace": "kust",
                "overlay": "prod",
                "name_prefix": "prod-",
                "base_name": "app",
                "image": "nginx:1.27",
                "replicas": 2,
            },
        },
    ]
}


def main() -> int:
    provider = StaticProvider(json.dumps(PAYLOAD))
    plan, _raw = generate_exam_plan(provider, topics=["all"], num_questions=len(PAYLOAD["questions"]))
    results = render_exam(plan)

    work_root = Path("/tmp/cka_e2e_work")
    env = MinikubeEnv(profile="cka-exam", addons=("metrics-server",))
    env.start(reset=True)

    wd = Workdir(work_root)
    exam_dir = wd.new_exam()
    kubeconfig = env.export_kubeconfig(exam_dir / "kubeconfig")
    kubectl = env.kubectl(kubeconfig)
    node_name = env.node_name(kubectl)
    wd.save_plan(exam_dir, plan)

    for result in results:
        print(f"--- setup Q{result.question_index} ({result.archetype_id}) ---")
        skip = {"Deployment"} if result.archetype_id == "troubleshooting_crashloop" else None
        apply_result_setup(kubectl, result, node_name)
        for warning in wait_for_manifests(kubectl, result.setup_manifests, skip_kinds=skip):
            print("  setup warn:", warning)
        try:
            report = preflight_result(kubectl, result, node_name)
        except PreflightError as exc:
            print(f"  PREFLIGHT FAILED: {exc}")
            env.delete()
            return 1
        for warning in report.warnings:
            print("  preflight warn:", warning)
        print("  preflight OK")

    print("\n=== simulating the user solving via reference solutions ===")
    for result in results:
        apply_reference(kubectl, result, node_name)
        wait_for_manifests(kubectl, result.reference_manifests, timeout=240)
        if not _wait_satisfied(kubectl, result, timeout=150):
            print(f"  Q{result.question_index} ({result.archetype_id}) never satisfied")
            env.delete()
            return 1
        print(f"  Q{result.question_index} ({result.archetype_id}) satisfied")

    grade = grade_exam(results, kubectl.run)
    print(f"\nGRADE: {grade.passed_checks}/{grade.total_checks} ({grade.fraction * 100:.1f}%)")
    env.delete()
    return 0 if grade.fraction == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
