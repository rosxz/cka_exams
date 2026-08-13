from __future__ import annotations

import json

import pytest

from cka_mock.generation import generate_exam_plan
from cka_mock.providers import SequenceProvider, StaticProvider
from cka_mock.schemas import GenerationError

VALID_PAYLOAD = {
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
            "archetype": "pvc",
            "params": {
                "name": "data",
                "namespace": "db",
                "access_mode": "ReadWriteOnce",
                "size": "100Mi",
                "storage_class": "standard",
            },
        },
    ]
}


def _valid_json() -> str:
    return json.dumps(VALID_PAYLOAD)


def test_generates_plan_from_valid_output():
    provider = StaticProvider(_valid_json())
    plan, raw = generate_exam_plan(provider, topics=["storage"], num_questions=2)
    assert len(plan.questions) == 2
    assert plan.questions[0].archetype_id == "deployment"
    assert plan.meta["provider_retries"] == 0
    assert raw == _valid_json()


def test_retries_on_invalid_then_valid():
    invalid = json.dumps(
        {
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
    )
    provider = SequenceProvider([invalid, _valid_json()])
    plan, raw = generate_exam_plan(provider, topics=[], num_questions=2)
    assert len(plan.questions) == 2
    assert plan.meta["provider_retries"] == 1


def test_fails_after_max_retries():
    invalid = '{"questions": [{"archetype": "nope", "params": {}}]}'
    provider = StaticProvider(invalid)
    with pytest.raises(GenerationError) as exc_info:
        generate_exam_plan(provider, topics=[], num_questions=1, max_retries=3)
    assert "unknown archetype" in str(exc_info.value)


def test_fails_on_non_json():
    provider = StaticProvider("not json at all")
    with pytest.raises(GenerationError):
        generate_exam_plan(provider, topics=[], num_questions=1, max_retries=2)


def test_prompt_mentions_topics_and_constraints(capsys=None):
    from cka_mock.generation import build_generation_prompt

    prompt = build_generation_prompt(
        topics=["RBAC", "Storage"], num_questions=3, difficulty="hard", fingerprints=["x"]
    )
    assert "RBAC" in prompt
    assert "Storage" in prompt
    assert "exactly 3 questions" in prompt
    assert "difficulty" in prompt.lower()
    assert "^[a-z0-9]" in prompt
    assert "x" in prompt  # fingerprint surfaced


def test_prompt_lists_rejected_questions():
    from cka_mock.generation import build_generation_prompt

    prompt = build_generation_prompt(
        topics=[],
        num_questions=2,
        difficulty="medium",
        rejected=[{"archetype": "configmap_secret", "params": {"name": "cfg", "namespace": "app"}}],
    )
    assert "REJECTED" in prompt
    assert "configmap_secret" in prompt
    assert "cfg" in prompt
    assert "Do NOT recreate" in prompt


def test_prompt_mentions_family_cap_and_unique_hosts():
    from cka_mock.generation import build_generation_prompt

    prompt = build_generation_prompt(
        topics=[],
        num_questions=4,
        difficulty="medium",
        max_per_family=3,
    )
    assert "At most 3 questions" in prompt
    assert "ingress" in prompt and "ingress_multi" in prompt
    assert "All Ingress host names must be unique" in prompt


def test_generate_replacement_question_valid():
    from cka_mock.generation import generate_replacement_question

    provider = StaticProvider(json.dumps({
        "questions": [{
            "archetype": "deployment",
            "params": {
                "name": "new-app", "namespace": "app",
                "image": "nginx:1.27", "replicas": 1, "labels": {"app": "new-app"},
            },
        }]
    }))
    q = generate_replacement_question(
        provider,
        existing_questions=[],
        failed_question={"archetype": "configmap_secret", "params": {}},
    )
    assert q.archetype_id == "deployment"
    assert q.params["name"] == "new-app"


def test_generate_replacement_question_avoids_collisions():
    from cka_mock.generation import generate_replacement_question

    existing = [{
        "archetype": "deployment",
        "params": {"name": "app", "namespace": "n", "image": "nginx:1.27", "replicas": 1, "labels": {"app": "a"}},
    }]
    colliding = json.dumps({"questions": [{
        "archetype": "deployment",
        "params": {"name": "app", "namespace": "n", "image": "nginx:1.27", "replicas": 1, "labels": {"app": "a"}},
    }]})
    valid = json.dumps({"questions": [{
        "archetype": "deployment",
        "params": {"name": "other", "namespace": "other", "image": "nginx:1.27", "replicas": 1, "labels": {"app": "a"}},
    }]})
    q = generate_replacement_question(
        SequenceProvider([colliding, valid]),
        existing_questions=existing,
        failed_question={"archetype": "deployment", "params": {}},
    )
    assert q.params["name"] == "other"


def test_generate_replacement_question_requires_exactly_one():
    from cka_mock.generation import generate_replacement_question

    provider = StaticProvider(json.dumps({"questions": [
        {
            "archetype": "deployment",
            "params": {"name": "a", "namespace": "n", "image": "nginx:1.27", "replicas": 1, "labels": {"app": "a"}},
        },
        {
            "archetype": "pvc",
            "params": {"name": "b", "namespace": "n", "access_mode": "ReadWriteOnce", "size": "100Mi", "storage_class": "standard"},
        },
    ]}))
    with pytest.raises(GenerationError):
        generate_replacement_question(
            provider, existing_questions=[], failed_question={}, max_attempts=1
        )
