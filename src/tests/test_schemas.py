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


def test_parse_json_strips_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('{"a": 1}') == {"a": 1}
