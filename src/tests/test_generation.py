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
