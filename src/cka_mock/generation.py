"""LLM exam generation: prompt construction, parsing, validation, retry loop.

The LLM only emits structured JSON selecting archetypes and parameterizing them.
It never produces manifests, files, or commands.
"""
from __future__ import annotations

import json

from .archetypes import REGISTRY, summarize_schema
from .providers import Provider
from .schemas import (
    ExamPlan,
    GenerationError,
    parse_json,
    payload_to_plan,
    validate_exam_payload,
)


def build_generation_prompt(
    *,
    topics: list[str],
    num_questions: int,
    difficulty: str,
    fingerprints: list[str] | None = None,
) -> str:
    lines = [
        "You generate mock CKA exam challenges as JSON.",
        "Return ONLY valid JSON. No markdown fences, no commentary.",
        'The JSON shape: {"questions": [{"archetype": "<archetype id>", "params": { ... }}]}.',
        "Each question must be self-contained and verifiable via kubectl.",
        "",
        f"Select exactly {num_questions} questions. Difficulty: {difficulty}.",
    ]
    if topics:
        lines.append(
            f"The student flagged these topics as weak; focus the selection on them: "
            f"{', '.join(topics)}."
        )
    lines.append("")
    lines.append("AVAILABLE ARCHETYPES:")
    for arch in REGISTRY.values():
        lines.append(f"- {arch.id}: {arch.title} [{arch.domain}] topics={', '.join(arch.topics)}")
        lines.append(f"    {arch.description}")
        lines.append(f"    params: {summarize_schema(arch.params_schema)}")
    if fingerprints:
        lines.append("")
        lines.append(
            "Avoid reusing these previously-used parameter sets (names, namespaces, images):"
        )
        for fp in fingerprints:
            lines.append(f"- {fp}")
    lines.append("")
    lines.append(
        "Constraints: names/namespaces must match ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$; images only "
        "from the allowed list; ports in 1..65535. Use realistic, varied names and namespaces."
    )
    return "\n".join(lines)


def build_fix_prompt(previous_prompt: str, raw_output: str, errors: list[str]) -> str:
    lines = [
        "The previous response failed validation. Return ONLY the corrected JSON document.",
        "Fix the reported errors minimally; keep the rest unchanged.",
        "Validation errors:",
    ]
    for error in errors:
        lines.append(f"- {error}")
    lines.append("\nPREVIOUS_INSTRUCTIONS:\n" + previous_prompt)
    lines.append("\nPREVIOUS_RESPONSE:\n" + raw_output)
    return "\n".join(lines)


def generate_exam_plan(
    provider: Provider,
    *,
    topics: list[str],
    num_questions: int = 17,
    difficulty: str = "medium",
    fingerprints: list[str] | None = None,
    max_retries: int = 3,
) -> tuple[ExamPlan, str]:
    """Ask the provider for an exam plan, validating with fail-closed retries.

    Returns ``(plan, raw_output)`` so the raw LLM output is auditable.
    Raises :class:`GenerationError` if validation never passes.
    """
    prompt = build_generation_prompt(
        topics=topics,
        num_questions=num_questions,
        difficulty=difficulty,
        fingerprints=fingerprints,
    )
    errors: list[str] = []
    raw_output = ""

    for attempt in range(max_retries):
        prompt_text = build_fix_prompt(prompt, raw_output, errors) if attempt else prompt
        result = provider.generate(prompt_text)
        raw_output = result.text

        try:
            payload = parse_json(raw_output)
        except json.JSONDecodeError as exc:
            errors = [f"response was not valid JSON: {exc}"]
            continue

        if not isinstance(payload, dict):
            errors = ["response must be a JSON object"]
            continue

        errors = validate_exam_payload(payload, REGISTRY)
        if not errors:
            plan = payload_to_plan(payload, REGISTRY)
            plan.meta = {
                "provider_retries": attempt,
                "difficulty": difficulty,
                "topics": list(topics),
            }
            return plan, raw_output

    raise GenerationError("; ".join(errors))
