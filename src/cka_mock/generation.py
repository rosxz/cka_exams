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
    QuestionSpec,
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
    rejected: list[dict] | None = None,
    max_per_family: int = 3,
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
    if rejected:
        lines.append("")
        lines.append(
            "These previously-attempted questions were REJECTED (their setups/preflights "
            "failed). Do NOT recreate them or near-variants. Replace them with different "
            "archetypes and different names/namespaces/params:"
        )
        for question in rejected:
            lines.append(f"- {question.get('archetype')} {json.dumps(question.get('params', {}), sort_keys=True)}")
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
        "from the allowed list; ports in 1..65535; ConfigMap/Secret data keys must be valid "
        "environment-variable names (^[A-Za-z_][A-Za-z0-9_]*$). Use realistic, varied names "
        "and namespaces."
    )
    lines.append(
        f"At most {max_per_family} questions may come from the same family (the 'ingress' family "
        f"includes both 'ingress' and 'ingress_multi'). All Ingress host names must be unique "
        f"across the exam. CertificateSigningRequest names must be unique across the exam. "
        f"Diversify archetypes."
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


def build_replacement_prompt(
    *,
    existing_questions: list[dict],
    failed_question: dict,
    max_per_family: int,
) -> str:
    lines = [
        "You are replacing ONE question in a CKA mock exam that failed at runtime.",
        "Return ONLY valid JSON with exactly one question: "
        '{"questions": [{"archetype": "<archetype id>", "params": { ... }}]}.',
        "No markdown fences, no commentary.",
        "",
        "The rest of the exam (do NOT duplicate any of these names/namespaces/hosts):",
    ]
    for index, question in enumerate(existing_questions, start=1):
        lines.append(
            f"- Q{index}: {question.get('archetype')} "
            f"{json.dumps(question.get('params', {}), sort_keys=True)}"
        )
    lines.append("")
    lines.append(
        "The question being replaced FAILED and must NOT be recreated or near-duplicated:"
    )
    lines.append(
        f"- {failed_question.get('archetype')} "
        f"{json.dumps(failed_question.get('params', {}), sort_keys=True)}"
    )
    lines.append("")
    lines.append("Constraints:")
    lines.append(
        f"- At most {max_per_family} questions may come from the same family "
        "(the 'ingress' family = ingress + ingress_multi)."
    )
    lines.append(
        "- All names/namespaces must match ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$; Ingress hosts must "
        "be unique DNS names; images from the allowed list; ConfigMap/Secret keys valid env names."
    )
    return "\n".join(lines)


def generate_replacement_question(
    provider: Provider,
    *,
    existing_questions: list[dict],
    failed_question: dict,
    max_per_family: int = 3,
    max_attempts: int = 3,
) -> QuestionSpec:
    """Ask the provider for a single replacement question.

    The candidate (existing questions + the replacement) is validated as a whole,
    so name/host uniqueness and family caps hold across the full exam.
    Raises :class:`GenerationError` if no valid replacement is produced.
    """
    prompt = build_replacement_prompt(
        existing_questions=existing_questions,
        failed_question=failed_question,
        max_per_family=max_per_family,
    )
    errors: list[str] = []
    raw_output = ""

    for attempt in range(max_attempts):
        prompt_text = build_fix_prompt(prompt, raw_output, errors) if attempt else prompt
        result = provider.generate(prompt_text)
        raw_output = result.text

        try:
            payload = parse_json(raw_output)
        except json.JSONDecodeError as exc:
            errors = [f"response was not valid JSON: {exc}"]
            continue

        questions = payload.get("questions") if isinstance(payload, dict) else None
        if not isinstance(questions, list) or len(questions) != 1:
            errors = ["must return exactly 1 question"]
            continue

        candidate = existing_questions + questions
        errors = validate_exam_payload(
            {"questions": candidate}, REGISTRY, max_per_family=max_per_family
        )
        if not errors:
            question = questions[0]
            return QuestionSpec(archetype_id=question["archetype"], params=question["params"])

    raise GenerationError("; ".join(errors))


def generate_exam_plan(
    provider: Provider,
    *,
    topics: list[str],
    num_questions: int = 17,
    difficulty: str = "medium",
    fingerprints: list[str] | None = None,
    rejected: list[dict] | None = None,
    max_per_family: int = 3,
    max_retries: int = 3,
) -> tuple[ExamPlan, str]:
    """Ask the provider for an exam plan, validating with fail-closed retries.

    ``rejected`` lists question dicts (archetype + params) from failed attempts
    that must not be recreated. Returns ``(plan, raw_output)`` so the raw LLM
    output is auditable. Raises :class:`GenerationError` if validation never
    passes.
    """
    prompt = build_generation_prompt(
        topics=topics,
        num_questions=num_questions,
        difficulty=difficulty,
        fingerprints=fingerprints,
        rejected=rejected,
        max_per_family=max_per_family,
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

        errors = validate_exam_payload(payload, REGISTRY, max_per_family=max_per_family)
        if not errors:
            plan = payload_to_plan(payload, REGISTRY)
            plan.meta = {
                "provider_retries": attempt,
                "difficulty": difficulty,
                "topics": list(topics),
            }
            return plan, raw_output

    raise GenerationError("; ".join(errors))
