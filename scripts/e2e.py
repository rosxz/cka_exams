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
from cka_mock.exam import _write_exam_files
from cka_mock.generation import generate_exam_plan
from cka_mock.grader import grade_exam
from cka_mock.preflight import PreflightError, _wait_ready, _wait_satisfied, preflight_result
from cka_mock.providers import StaticProvider
from cka_mock.renderer import render_exam
from cka_mock.setup import (
    apply_install_commands,
    apply_reference,
    apply_result_setup,
    wait_for_manifests,
)
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
        {
            "archetype": "ingress",
            "params": {
                "name": "web-ing",
                "namespace": "ingressapp",
                "host": "web.example.com",
                "marker": "backend-web-marker",
                "labels": {"app": "web"},
                "ingress_class": "nginx",
                "port": 80,
            },
        },
        {
            "archetype": "ingress_multi",
            "params": {
                "name": "shop-ing",
                "namespace": "shop",
                "host_a": "shop.example.com",
                "host_b": "admin.example.com",
                "marker_a": "shop-marker",
                "marker_b": "admin-marker",
                "labels_a": {"app": "shop"},
                "labels_b": {"app": "admin"},
                "ingress_class": "nginx",
                "port": 80,
            },
        },
        {
            "archetype": "crd",
            "params": {
                "plural": "widgets",
                "singular": "widget",
                "group": "example.com",
                "version": "v1",
                "kind": "Widget",
                "scope": "Namespaced",
                "spec_field": "size",
                "spec_type": "string",
                "instance_name": "widget-one",
                "instance_namespace": "widgets",
                "instance_value": "large",
            },
        },
        {
            "archetype": "operator",
            "params": {
                "cert_name": "web-cert",
                "namespace": "certapp",
                "issuer_name": "self-issuer",
                "host": "cert.example.com",
            },
        },
        {
            "archetype": "gateway",
            "params": {
                "name": "web-route",
                "namespace": "gwapp",
                "host": "gw.example.com",
                "labels": {"app": "web"},
                "port": 80,
                "replicas": 1,
            },
        },
        {
            "archetype": "fix_deployment",
            "params": {
                "name": "web",
                "namespace": "fxdep",
                "image": "nginx:1.27",
                "wrong_image": "redis:7.2",
                "replicas": 2,
                "labels": {"app": "web"},
                "container_port": 80,
            },
        },
        {
            "archetype": "fix_service",
            "params": {
                "name": "web-svc",
                "namespace": "fxsvc",
                "service_type": "ClusterIP",
                "port": 80,
                "target_port": 8080,
                "backend_labels": {"app": "web"},
                "wrong_labels": {"app": "wrong"},
                "backend_image": "nginx:1.27",
                "backend_replicas": 1,
            },
        },
        {
            "archetype": "fix_pvc",
            "params": {
                "name": "data",
                "namespace": "fxpvc",
                "access_mode": "ReadWriteOnce",
                "size": "100Mi",
                "storage_class": "slowfix",
            },
        },
        {
            "archetype": "fix_networkpolicy",
            "params": {
                "name": "np",
                "namespace": "fxnp",
                "target_labels": {"app": "api"},
                "peer_labels": {"tier": "fe"},
                "blocked_labels": {"app": "blocked"},
                "port": 8000,
            },
        },
        {
            "archetype": "fix_scheduling",
            "params": {
                "name": "sched",
                "namespace": "fxsch",
                "image": "redis:7.2",
                "replicas": 2,
                "labels": {"app": "sched"},
                "node_label_key": "dedicated",
                "node_label_value": "gpu",
            },
        },
        {
            "archetype": "fix_configmap",
            "params": {
                "name": "cfg",
                "namespace": "fxcm",
                "deploy_name": "app",
                "image": "nginx:1.27",
                "labels": {"app": "app"},
                "replicas": 1,
                "env_key": "MODE",
                "correct_value": "prod",
                "wrong_value": "dev",
            },
        },
        {
            "archetype": "fix_ingress",
            "params": {
                "name": "web-ing",
                "namespace": "fxing",
                "host": "fixweb.example.com",
                "labels": {"app": "web"},
                "port": 80,
                "replicas": 1,
                "marker": "fx-marker",
            },
        },
        {
            "archetype": "fix_rbac",
            "params": {
                "sa_name": "deployer",
                "namespace": "fxrbac",
                "role_name": "deployer-role",
                "role_kind": "Role",
                "resources": ["deployments", "pods"],
                "verbs": ["get", "list", "create"],
                "wrong_resources": ["secrets"],
                "wrong_verbs": ["delete"],
            },
        },
        {
            "archetype": "csr",
            "params": {
                "csr_name": "user-csr",
                "cn": "dev-user",
                "namespace": "certns",
                "secret_name": "dev-tls",
            },
        },
        {
            "archetype": "validating_admission_policy",
            "params": {
                "policy_name": "req-label",
                "binding_name": "req-label-bind",
                "namespace": "admons",
                "label_key": "env",
            },
        },
        {
            "archetype": "jsonpath",
            "params": {
                "cm_name": "node-info",
                "cm_namespace": "jpath",
                "cm_key": "names",
                "query": "node_names",
            },
        },
        {
            "archetype": "fix_autoscaling",
            "params": {
                "name": "web-hpa",
                "namespace": "fxhpa",
                "workload": "web",
                "min": 1,
                "max": 5,
                "cpu_target": 50,
                "image": "nginx:1.27",
                "replicas": 1,
                "labels": {"app": "web"},
                "wrong_min": 2,
                "wrong_max": 10,
            },
        },
    ]
}


def main() -> int:
    provider = StaticProvider(json.dumps(PAYLOAD))
    plan, _raw = generate_exam_plan(provider, topics=["all"], num_questions=len(PAYLOAD["questions"]))
    results = render_exam(plan)

    from cka_mock.config import Config
    from cka_mock.exam import _needed_addons

    addons = _needed_addons(Config(addons=["metrics-server"]), results)
    work_root = Path("/tmp/cka_e2e_work")
    env = MinikubeEnv(profile="cka-exam", addons=tuple(addons))
    env.start(reset=False, log=lambda m: print(m), preload_images=True)

    wd = Workdir(work_root)
    exam_dir = wd.new_exam()
    kubeconfig = env.export_kubeconfig(exam_dir / "kubeconfig")
    kubectl = env.kubectl(kubeconfig)
    node_name = env.node_name(kubectl)
    wd.save_plan(exam_dir, plan)
    _write_exam_files(exam_dir, results)
    files_dir = exam_dir / "files"

    for result in results:
        print(f"--- setup Q{result.question_index} ({result.archetype_id}) ---")
        apply_result_setup(kubectl, result, node_name, files_dir)
        for warning in wait_for_manifests(
            kubectl, result.setup_manifests, skip_objects=result.setup_waits_skip
        ):
            print("  setup warn:", warning)
        try:
            report = preflight_result(kubectl, result, node_name, files_dir=files_dir)
        except PreflightError as exc:
            print(f"  PREFLIGHT FAILED: {exc}")
            env.delete()
            return 1
        for warning in report.warnings:
            print("  preflight warn:", warning)
        print("  preflight OK")

    print("\n=== simulating the user solving via reference solutions ===")
    for result in results:
        apply_install_commands(kubectl, result, node_name, files_dir)
        if result.reference_ready_assertions:
            if not _wait_ready(kubectl, result.reference_ready_assertions, timeout=360):
                print(f"  Q{result.question_index} ({result.archetype_id}) install never ready")
                env.delete()
                return 1
        apply_reference(kubectl, result, node_name, files_dir)
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
