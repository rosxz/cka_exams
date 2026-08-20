from __future__ import annotations

from cka_mock.archetypes import REGISTRY
from cka_mock.schemas import (
    GENERATION_SCHEMA,
    IMAGE_ALLOWLIST,
    parse_json,
    validate_exam_payload,
)

VALID = {
    "questions": [
        {
            "archetype": "deployment",
            "params": {
                "name": "web",
                "namespace": "frontend",
                "image": "nginx:1.27",
                "replicas": 3,
                "labels": {"app": "web"},
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
            },
        },
        {
            "archetype": "pvc",
            "params": {
                "name": "data",
                "namespace": "db",
                "access_mode": "ReadWriteOnce",
                "size": "100Mi",
                "storage_class": "standard",
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
            "archetype": "networkpolicy",
            "params": {
                "name": "allow-api",
                "namespace": "api",
                "target_labels": {"app": "api"},
                "peer_labels": {"tier": "frontend"},
                "port": 8443,
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
    ]
}


def test_schema_requires_questions():
    errors = validate_exam_payload({}, REGISTRY)
    assert any("questions" in e for e in errors)


def test_valid_payload_passes():
    assert validate_exam_payload(VALID, REGISTRY) == []


def test_unknown_archetype():
    payload = {
        "questions": [
            {"archetype": "does_not_exist", "params": {"name": "x", "namespace": "y"}}
        ]
    }
    errors = validate_exam_payload(payload, REGISTRY)
    assert any("unknown archetype" in e for e in errors)


def test_bad_name_rejected():
    payload = {
        "questions": [
            {
                "archetype": "deployment",
                "params": {
                    "name": "Bad_Name",
                    "namespace": "frontend",
                    "image": "nginx:1.27",
                    "replicas": 1,
                    "labels": {"app": "web"},
                },
            }
        ]
    }
    errors = validate_exam_payload(payload, REGISTRY)
    assert any("Bad_Name" in e for e in errors)


def test_image_must_be_allowlisted():
    payload = {
        "questions": [
            {
                "archetype": "deployment",
                "params": {
                    "name": "web",
                    "namespace": "frontend",
                    "image": "evil:latest",
                    "replicas": 1,
                    "labels": {"app": "web"},
                },
            }
        ]
    }
    errors = validate_exam_payload(payload, REGISTRY)
    assert any("evil:latest" in e for e in errors)


def test_missing_required_param():
    payload = {
        "questions": [
            {
                "archetype": "deployment",
                "params": {"name": "web", "namespace": "frontend", "image": "nginx:1.27"},
            }
        ]
    }
    errors = validate_exam_payload(payload, REGISTRY)
    assert any("replicas" in e for e in errors)


def test_extra_params_rejected():
    payload = {
        "questions": [
            {
                "archetype": "deployment",
                "params": {
                    "name": "web",
                    "namespace": "frontend",
                    "image": "nginx:1.27",
                    "replicas": 1,
                    "labels": {"app": "web"},
                    "surprise": "field",
                },
            }
        ]
    }
    errors = validate_exam_payload(payload, REGISTRY)
    assert any("surprise" in e for e in errors)


def test_duplicate_names_rejected():
    payload = {
        "questions": [
            {
                "archetype": "deployment",
                "params": {
                    "name": "web",
                    "namespace": "frontend",
                    "image": "nginx:1.27",
                    "replicas": 1,
                    "labels": {"a": "b"},
                },
            },
            {
                "archetype": "service",
                "params": {
                    "name": "web",
                    "namespace": "frontend",
                    "service_type": "ClusterIP",
                    "port": 80,
                    "target_port": 80,
                    "backend_labels": {"a": "b"},
                },
            },
        ]
    }
    errors = validate_exam_payload(payload, REGISTRY)
    assert any("duplicate" in e for e in errors)


def test_every_archetype_has_valid_schema():
    from jsonschema import Draft202012Validator

    for arch in REGISTRY.values():
        Draft202012Validator.check_schema(arch.params_schema)
    Draft202012Validator.check_schema(GENERATION_SCHEMA)


def test_image_allowlist_is_curated():
    assert IMAGE_ALLOWLIST


def test_cm_secret_keys_must_be_valid_env_names():
    base = {
        "archetype": "configmap_secret",
        "params": {
            "name": "cfg",
            "namespace": "app",
            "deploy_name": "app",
            "image": "nginx:1.27",
            "replicas": 1,
            "labels": {"app": "app"},
        },
    }
    bad_key = {**base, "params": {**base["params"], "cm_data": {"bad.key": "v"}}}
    errors = validate_exam_payload({"questions": [bad_key]}, REGISTRY)
    assert any("bad.key" in e for e in errors)

    bad_start = {**base, "params": {**base["params"], "secret_data": {"1start": "v"}}}
    errors = validate_exam_payload({"questions": [bad_start]}, REGISTRY)
    assert any("1start" in e for e in errors)

    good = {**base, "params": {**base["params"], "cm_data": {"FOO_1": "v"}, "secret_data": {"bar": "v"}}}
    assert validate_exam_payload({"questions": [good]}, REGISTRY) == []


def test_ingress_host_must_be_valid_dns():
    base = {
        "archetype": "ingress",
        "params": {
            "name": "ing",
            "namespace": "app",
            "marker": "m",
            "labels": {"app": "a"},
        },
    }
    good = {**base, "params": {**base["params"], "host": "web.example.com"}}
    assert validate_exam_payload({"questions": [good]}, REGISTRY) == []

    no_dot = {**base, "params": {**base["params"], "host": "nohost"}}
    assert validate_exam_payload({"questions": [no_dot]}, REGISTRY) != []

    bad_chars = {**base, "params": {**base["params"], "host": "bad_host.example.com"}}
    assert validate_exam_payload({"questions": [bad_chars]}, REGISTRY) != []

    bad_class = {**base, "params": {**base["params"], "host": "web.example.com", "ingress_class": "Bad!"}}
    assert validate_exam_payload({"questions": [bad_class]}, REGISTRY) != []


def _ingress_q(name, ns, host):
    return {
        "archetype": "ingress",
        "params": {
            "name": name,
            "namespace": ns,
            "host": host,
            "marker": "m",
            "labels": {"app": "a"},
        },
    }


def test_family_cap_limits_ingress_questions():
    questions = [_ingress_q(f"ing{i}", f"ns{i}", f"h{i}.example.com") for i in range(4)]
    errors = validate_exam_payload({"questions": questions}, REGISTRY, max_per_family=3)
    assert any("too many questions" in e and "ingress" in e for e in errors)
    assert validate_exam_payload({"questions": questions[:3]}, REGISTRY, max_per_family=3) == []


def test_ingress_hosts_must_be_unique():
    questions = [
        _ingress_q("a", "n1", "app.example.com"),
        _ingress_q("b", "n2", "app.example.com"),
    ]
    errors = validate_exam_payload({"questions": questions}, REGISTRY)
    assert any("app.example.com" in e and "another" in e for e in errors)


def test_ingress_multi_host_a_b_must_differ():
    questions = [
        {
            "archetype": "ingress_multi",
            "params": {
                "name": "s",
                "namespace": "n",
                "host_a": "x.example.com",
                "host_b": "x.example.com",
                "marker_a": "a",
                "marker_b": "b",
                "labels_a": {"app": "a"},
                "labels_b": {"app": "b"},
            },
        }
    ]
    errors = validate_exam_payload({"questions": questions}, REGISTRY)
    assert any("host_a" in e for e in errors)


def test_ingress_multi_hosts_join_uniqueness():
    questions = [
        _ingress_q("a", "n1", "app.example.com"),
        {
            "archetype": "ingress_multi",
            "params": {
                "name": "s",
                "namespace": "n2",
                "host_a": "app.example.com",
                "host_b": "other.example.com",
                "marker_a": "a",
                "marker_b": "b",
                "labels_a": {"app": "a"},
                "labels_b": {"app": "b"},
            },
        },
    ]
    errors = validate_exam_payload({"questions": questions}, REGISTRY)
    assert any("app.example.com" in e and "another" in e for e in errors)


def test_workload_images_must_stay_running():
    """busybox/alpine/python exit immediately and must not be used for workloads
    whose Deployments must become Ready."""
    base_deploy = {
        "archetype": "deployment",
        "params": {
            "name": "web", "namespace": "app", "replicas": 1, "labels": {"app": "web"},
        },
    }
    for bad in ("busybox:1.36", "alpine:3.20", "python:3.12-slim"):
        q = {**base_deploy, "params": {**base_deploy["params"], "image": bad}}
        assert validate_exam_payload({"questions": [q]}, REGISTRY) != [], bad
    for good in ("nginx:1.27", "redis:7.2", "httpd:2.4", "memcached:1.6"):
        q = {**base_deploy, "params": {**base_deploy["params"], "image": good}}
        assert validate_exam_payload({"questions": [q]}, REGISTRY) == [], good


def test_crashloop_image_must_serve_http():
    base = {
        "archetype": "troubleshooting_crashloop",
        "params": {
            "name": "broken", "namespace": "ops", "labels": {"app": "broken"},
            "replicas": 1, "failure": "bad_liveness",
        },
    }
    for bad in ("busybox:1.36", "redis:7.2", "memcached:1.6"):
        q = {**base, "params": {**base["params"], "image": bad}}
        assert validate_exam_payload({"questions": [q]}, REGISTRY) != [], bad
    for good in ("nginx:1.27", "httpd:2.4"):
        q = {**base, "params": {**base["params"], "image": good}}
        assert validate_exam_payload({"questions": [q]}, REGISTRY) == [], good


def test_ingress_backend_image_not_expected():
    q = {
        "archetype": "ingress",
        "params": {
            "name": "ing", "namespace": "app", "host": "web.example.com",
            "marker": "m", "labels": {"app": "a"}, "backend_image": "nginx:1.27",
        },
    }
    assert validate_exam_payload({"questions": [q]}, REGISTRY) != []  # extra param rejected


def test_fix_archetypes_require_distinct_broken_and_correct_values():
    fix_deployment = {
        "archetype": "fix_deployment",
        "params": {
            "name": "web", "namespace": "app", "image": "nginx:1.27", "replicas": 1,
            "labels": {"app": "web"}, "wrong_image": "nginx:1.27",
        },
    }
    assert validate_exam_payload({"questions": [fix_deployment]}, REGISTRY) != []

    fix_configmap = {
        "archetype": "fix_configmap",
        "params": {
            "name": "cfg", "namespace": "app", "deploy_name": "app", "image": "nginx:1.27",
            "labels": {"app": "app"}, "replicas": 1, "env_key": "MODE",
            "correct_value": "prod", "wrong_value": "prod",
        },
    }
    assert validate_exam_payload({"questions": [fix_configmap]}, REGISTRY) != []

    good = {
        "archetype": "fix_deployment",
        "params": {
            "name": "web", "namespace": "app", "image": "nginx:1.27",
            "wrong_image": "redis:7.2", "replicas": 1, "labels": {"app": "web"},
        },
    }
    assert validate_exam_payload({"questions": [good]}, REGISTRY) == []


def test_storage_class_must_be_unique_across_exam():
    pvc = {
        "archetype": "pvc",
        "params": {
            "name": "data", "namespace": "db", "access_mode": "ReadWriteOnce",
            "size": "100Mi", "storage_class": "slow",
        },
    }
    fix_pvc = {
        "archetype": "fix_pvc",
        "params": {
            "name": "data2", "namespace": "db2", "access_mode": "ReadWriteOnce",
            "size": "100Mi", "storage_class": "slow",
        },
    }
    errors = validate_exam_payload({"questions": [pvc, fix_pvc]}, REGISTRY)
    assert any("storage class" in e and "slow" in e for e in errors)


def test_parse_json_strips_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('{"a": 1}') == {"a": 1}


def test_csr_requires_required_params():
    base = {
        "archetype": "csr",
        "params": {"csr_name": "user-csr", "cn": "dev", "namespace": "ns", "secret_name": "tls"},
    }
    assert validate_exam_payload({"questions": [base]}, REGISTRY) == []
    for missing in ("csr_name", "cn", "namespace", "secret_name"):
        q = {**base, "params": {k: v for k, v in base["params"].items() if k != missing}}
        assert validate_exam_payload({"questions": [q]}, REGISTRY) != [], missing
    bad_csr = {**base, "params": {**base["params"], "csr_name": "Bad_Name"}}
    assert validate_exam_payload({"questions": [bad_csr]}, REGISTRY) != []


def test_csr_names_must_be_unique_across_exam():
    csr1 = {
        "archetype": "csr",
        "params": {"csr_name": "user-csr", "cn": "a", "namespace": "ns1", "secret_name": "s1"},
    }
    csr2 = {
        "archetype": "csr",
        "params": {"csr_name": "user-csr", "cn": "b", "namespace": "ns2", "secret_name": "s2"},
    }
    errors = validate_exam_payload({"questions": [csr1, csr2]}, REGISTRY)
    assert any("CSR name" in e and "user-csr" in e for e in errors)

    csr2 = {**csr2, "params": {**csr2["params"], "csr_name": "other-csr"}}
    assert validate_exam_payload({"questions": [csr1, csr2]}, REGISTRY) == []


def test_validating_admission_policy_requires_params():
    base = {
        "archetype": "validating_admission_policy",
        "params": {"policy_name": "req-label", "binding_name": "req-label-bind", "namespace": "ns", "label_key": "env"},
    }
    assert validate_exam_payload({"questions": [base]}, REGISTRY) == []
    bad_key = {**base, "params": {**base["params"], "label_key": "Bad Key"}}
    assert validate_exam_payload({"questions": [bad_key]}, REGISTRY) != []


def test_jsonpath_requires_valid_query():
    base = {
        "archetype": "jsonpath",
        "params": {"cm_name": "info", "cm_namespace": "ns", "cm_key": "names", "query": "node_names"},
    }
    assert validate_exam_payload({"questions": [base]}, REGISTRY) == []
    bad_query = {**base, "params": {**base["params"], "query": "not_a_real_query"}}
    assert validate_exam_payload({"questions": [bad_query]}, REGISTRY) != []
