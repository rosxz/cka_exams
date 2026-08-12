"""Validation schemas and constraints for the LLM generation contract.

The LLM output is a JSON document describing the exam. This module defines the
top-level schema, the curated image allowlist, k8s identifier constraints, and
helpers that turn raw JSON into a validated :class:`ExamPlan`.
"""
from __future__ import annotations

import json
import re

from dataclasses import dataclass, field

GENERATION_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "required": ["archetype", "params"],
                "properties": {
                    "archetype": {"type": "string"},
                    "params": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}

# Only these images may ever be scheduled. Keeps the environment predictable and
# prevents the LLM from pointing at arbitrary images.
IMAGE_ALLOWLIST: list[str] = [
    "nginx:1.27",
    "nginx:1.26",
    "nginx:1.25",
    "busybox:1.36",
    "busybox:1.35",
    "redis:7.2",
    "redis:6.2",
    "httpd:2.4",
    "memcached:1.6",
    "alpine:3.20",
    "python:3.12-slim",
]

# DNS-1123 subdomain, i.e. k8s object names.
K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# DNS-1123 label value.
LABEL_VALUE_RE = re.compile(r"^[a-zA-Z0-9]([-_.a-zA-Z0-9]*[a-zA-Z0-9])?$")
# DNS-1123 label key without an optional domain prefix.
LABEL_KEY_RE = re.compile(r"^[a-zA-Z0-9]([-_.a-zA-Z0-9]*[a-zA-Z0-9])?(\/[a-zA-Z0-9]([-_.a-zA-Z0-9]*[a-zA-Z0-9])?)?$")


def name_schema() -> dict:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 63,
        "pattern": K8S_NAME_RE.pattern,
    }


def label_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": {"type": "string", "pattern": LABEL_VALUE_RE.pattern},
        "propertyNames": {"pattern": LABEL_KEY_RE.pattern},
    }


def image_schema() -> dict:
    return {"type": "string", "enum": IMAGE_ALLOWLIST}


def parse_json(text: str) -> object:
    """Parse model output as JSON, tolerating fenced code blocks."""
    cleaned = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    return json.loads(cleaned)


@dataclass
class QuestionSpec:
    archetype_id: str
    params: dict


@dataclass
class ExamPlan:
    questions: list[QuestionSpec]
    meta: dict = field(default_factory=dict)


class GenerationError(Exception):
    pass


def _walk_errors(validator: object, instance: object) -> list[str]:
    from jsonschema import Draft202012Validator, ValidationError  # local import

    errors = list(Draft202012Validator(validator).iter_errors(instance))
    return [e.message for e in errors]


def validate_archetype_params(archetype, params: dict) -> list[str]:
    """Validate question params against an archetype's param schema."""
    schema = archetype.params_schema
    if not isinstance(params, dict):
        return ["params must be an object"]
    errors = _walk_errors(schema, params)
    extra = archetype.extra_checks(params) if archetype.extra_checks else []
    return errors + extra


def validate_exam_payload(payload: object, registry) -> list[str]:
    """Validate a full LLM generation payload. Returns human-readable errors."""
    from jsonschema import Draft202012Validator  # local import

    errors = _walk_errors(GENERATION_SCHEMA, payload)
    if errors:
        return errors
    if not isinstance(payload, dict):
        return ["payload must be an object"]

    questions = payload["questions"]
    seen: set[tuple[str, str]] = set()
    result: list[str] = []

    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            result.append(f"question {index}: must be an object")
            continue
        archetype_id = question.get("archetype")
        params = question.get("params")
        archetype = registry.get(archetype_id) if isinstance(archetype_id, str) else None
        if archetype is None:
            result.append(f"question {index}: unknown archetype {archetype_id!r}")
            continue
        param_errors = validate_archetype_params(archetype, params)
        for message in param_errors:
            result.append(f"question {index} ({archetype_id}): {message}")

        name = params.get("name")
        namespace = params.get("namespace")
        if isinstance(name, str) and isinstance(namespace, str):
            key = (namespace, name)
            if key in seen:
                result.append(f"question {index}: duplicate (namespace={namespace!r}, name={name!r})")
            seen.add(key)

    return result


def payload_to_plan(payload: object, registry) -> ExamPlan:
    if not isinstance(payload, dict) or "questions" not in payload:
        raise GenerationError("payload missing 'questions'")
    questions = []
    for q in payload["questions"]:
        questions.append(QuestionSpec(archetype_id=q["archetype"], params=q["params"]))
    return ExamPlan(questions=questions)
