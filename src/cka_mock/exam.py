"""High-level exam workflow: new / grade / reset / status / list / replay."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from rich.console import Console

from .config import Config
from .env import MinikubeEnv
from .grader import grade_exam
from .generation import generate_exam_plan, generate_replacement_question
from .history import add_fingerprints, load_fingerprints, record_grade
from .preflight import PreflightError, preflight_result
from .providers import build_provider
from .renderer import _dump, render_exam, render_task_markdown
from .report import render_text_report
from .setup import apply_result_setup, wait_for_manifests
from .workdir import Workdir

console = Console()


def _build_provider(cfg: Config):
    if cfg.provider == "opencode" and not cfg.api_key:
        raise RuntimeError(
            "OpenCode API key not set. Export OPENCODE_API_KEY (from https://opencode.ai/auth)."
        )
    return build_provider(
        name=cfg.provider,
        api_key=cfg.api_key,
        model=cfg.model,
        base_url=cfg.base_url,
        reasoning_effort=cfg.reasoning_effort,
    )


def run_new(cfg: Config, *, topics_override, questions_override, difficulty_override) -> int:
    topics = topics_override if topics_override is not None else cfg.topics
    questions = questions_override if questions_override is not None else cfg.questions
    difficulty = difficulty_override if difficulty_override is not None else cfg.difficulty

    provider = _build_provider(cfg)
    workdir = Workdir(cfg.workdir_root)
    attempts = max(1, cfg.exam_attempts)
    rejected: list[dict] = []
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        if attempt > 1:
            console.print(
                f"[yellow]Regenerating exam (attempt {attempt}/{attempts}) with a different "
                f"challenge set ...[/yellow]"
            )
        plan, raw = generate_exam_plan(
            provider,
            topics=topics,
            num_questions=questions,
            difficulty=difficulty,
            fingerprints=load_fingerprints(cfg.workdir_root),
            rejected=rejected or None,
            max_per_family=cfg.max_per_family,
        )
        add_fingerprints(
            cfg.workdir_root, [q.params | {"archetype": q.archetype_id} for q in plan.questions]
        )
        results = render_exam(plan)
        exam_dir = workdir.new_exam()
        try:
            return _run_exam_flow(cfg, plan, results, workdir, exam_dir, raw=raw, provider=provider)
        except KeyboardInterrupt:
            _cleanup_failed_exam(exam_dir)
            raise
        except Exception as exc:  # noqa: BLE001 - reported, then retried
            _cleanup_failed_exam(exam_dir)
            last_error = exc
            console.print(
                f"[yellow]Exam attempt {attempt} failed: {exc}[/yellow]"
            )
            rejected.extend(
                {"archetype": q.archetype_id, "params": q.params} for q in plan.questions
            )

    assert last_error is not None
    raise last_error


def run_replay(cfg: Config, exam_id: str) -> int:
    """Repeat a past exam as a fresh attempt: same questions, reset cluster,
    full setup + preflight. Deterministic — no LLM involved."""
    workdir = Workdir(cfg.workdir_root)
    source_dir = workdir.find(exam_id)
    plan_payload = workdir.load_plan(source_dir)
    plan = _plan_from_payload(plan_payload)
    results = render_exam(plan)

    exam_dir = workdir.new_exam()
    console.print(
        f"[bold]Repeating exam {exam_id}[/bold] ({len(results)} questions) as a fresh attempt ..."
    )
    try:
        return _run_exam_flow(cfg, plan, results, workdir, exam_dir, raw=None)
    except BaseException:
        _cleanup_failed_exam(exam_dir)
        raise


def _cleanup_failed_exam(exam_dir: Path) -> None:
    # An interrupted or failed `new`/`replay` should not leave a half-baked exam dir.
    if not (exam_dir / "exam.json").is_file():
        shutil.rmtree(exam_dir, ignore_errors=True)


def _cleanup_question_setup(kubectl, result, files_dir: Path | None = None) -> None:
    """Best-effort removal of a replaced question's setup artifacts.

    Namespaces are left in place (they may be shared with other questions and
    deleting them risks a delete/recreate race); only the question's own objects
    are removed.
    """
    for doc in result.setup_manifests:
        if doc.get("kind") == "Namespace":
            continue
        kubectl.run(
            ["delete", "-f", "-", "--ignore-not-found"],
            input=_dump(doc),
            timeout=120,
        )


def _repair_question(provider, plan, failed_index: int, *, max_per_family: int):
    """Ask the LLM for a replacement for the failing question, keeping the rest."""
    existing = [
        {"archetype": q.archetype_id, "params": q.params}
        for i, q in enumerate(plan.questions)
        if i != failed_index
    ]
    failed = {
        "archetype": plan.questions[failed_index].archetype_id,
        "params": plan.questions[failed_index].params,
    }
    return generate_replacement_question(
        provider,
        existing_questions=existing,
        failed_question=failed,
        max_per_family=max_per_family,
    )


_INGRESS_ARCHETYPES = ("ingress", "ingress_multi")


def _needed_addons(cfg: Config, results) -> list[str]:
    """Baseline addons from config, plus ingress whenever the exam needs it."""
    addons = list(cfg.addons)
    if any(r.archetype_id in _INGRESS_ARCHETYPES for r in results) and "ingress" not in addons:
        addons.append("ingress")
    return addons


def _run_exam_flow(cfg, plan, results, workdir, exam_dir, *, raw, provider=None) -> int:
    env = MinikubeEnv(
        profile=cfg.minikube_profile,
        cpus=cfg.minikube_cpus,
        memory=cfg.minikube_memory,
        cni=cfg.minikube_cni,
        addons=tuple(_needed_addons(cfg, results)),
    )
    console.print(f"[bold]Preparing minikube profile[/bold] {cfg.minikube_profile} ...")
    env.start(reset=True, log=lambda msg: console.print(msg))

    kubeconfig = env.export_kubeconfig(exam_dir / "kubeconfig")
    kubectl = env.kubectl(kubeconfig)
    node_name = env.node_name(kubectl)

    workdir.save_plan(exam_dir, plan)
    workdir.write(exam_dir, "questions.md", render_task_markdown(results))
    if raw is not None:
        workdir.write(exam_dir, "generation.json", raw)
    _write_exam_files(exam_dir, results)

    if any(r.archetype_id == "helm" for r in results) and not shutil.which("helm"):
        console.print("[yellow]warn[/yellow] this exam includes a Helm challenge but `helm` is not on PATH")

    console.print(f"[bold]Cluster ready[/bold] (node {node_name}). Setting up challenges...")
    files_dir = exam_dir / "files"
    preflight_warnings, repaired = _setup_questions(
        cfg, plan, results, provider, kubectl, node_name, files_dir, log=console.print
    )

    if repaired:
        workdir.save_plan(exam_dir, plan)
        workdir.write(exam_dir, "questions.md", render_task_markdown(results))
        _write_exam_files(exam_dir, results)

    workdir.write(exam_dir, "preflight.json", json.dumps({"warnings": preflight_warnings}, indent=2))

    console.print()
    console.print("[bold]Exam ready![/bold]")
    console.print(f"  Exam dir : {exam_dir}")
    console.print(f"  Questions: {len(results)}")
    console.print(f"  Duration : {cfg.duration_minutes} min")
    console.print(f"  Context  : {cfg.minikube_profile}")
    if repaired:
        console.print(
            f"  [yellow]Note[/yellow]: {len(repaired)} question(s) were regenerated in place: "
            f"Q{', Q'.join(str(i + 1) for i in repaired)}"
        )
    console.print()
    console.print("  Solve the questions in your terminal (copy-paste):")
    console.print(f"    pushd {exam_dir}; export KUBECONFIG=$PWD/kubeconfig")
    console.print("    cat questions.md")
    console.print("    ls files   # provided files for file-based challenges")
    console.print("  When done, grade with:  cka-mock grade")
    return 0


def _setup_questions(
    cfg, plan, results, provider, kubectl, node_name, files_dir, *, log
) -> tuple[list[str], list[int]]:
    """Apply setup and preflight each question, repairing failures in place.

    When a question fails preflight and an LLM provider is available, that one
    question is regenerated and retried here — the rest of the exam is kept.
    Returns ``(preflight_warnings, repaired_indices)``.
    """
    preflight_warnings: list[str] = []
    repaired: list[int] = []

    for index, result in enumerate(results):
        repaired_this_question = False
        repairs_left = max(0, cfg.repair_attempts) if provider is not None else 0
        while True:
            log(f"  Q{result.question_index}: {result.archetype_id} ...")
            skip_kinds = {"Deployment"} if result.archetype_id == "troubleshooting_crashloop" else None
            apply_result_setup(kubectl, result, node_name, files_dir)
            setup_warnings = wait_for_manifests(kubectl, result.setup_manifests, skip_kinds=skip_kinds)
            for warning in setup_warnings:
                log(f"    [yellow]warn[/yellow] Q{result.question_index}: {warning}")
            try:
                report = preflight_result(kubectl, result, node_name, files_dir=files_dir)
                break
            except PreflightError as exc:
                if repairs_left <= 0:
                    raise
                repairs_left -= 1
                log(
                    f"    [yellow]Q{result.question_index} failed preflight: {exc}[/yellow]"
                )
                log(
                    f"    regenerating just this challenge ({repairs_left + 1} repair attempt(s) left) ..."
                )
                try:
                    replacement = _repair_question(
                        provider, plan, index, max_per_family=cfg.max_per_family
                    )
                except Exception as rep_exc:  # noqa: BLE001
                    raise PreflightError(
                        f"Q{result.question_index}: could not generate a replacement: {rep_exc}"
                    ) from rep_exc
                plan.questions[index] = replacement
                repaired_this_question = True
                new_result = _render_single(replacement, index + 1)
                _cleanup_question_setup(kubectl, result, files_dir)
                _write_result_files(files_dir, new_result)
                results[index] = new_result
                result = new_result
        preflight_warnings.extend(report.warnings)
        for warning in report.warnings:
            log(f"    [yellow]preflight[/yellow] Q{result.question_index}: {warning}")
        if repaired_this_question:
            repaired.append(index)

    return preflight_warnings, repaired


def _render_single(question, question_index: int):
    from .renderer import RENDERERS

    renderer = RENDERERS[question.archetype_id]
    result = renderer(question)
    result.question_index = question_index
    return result


def _write_exam_files(exam_dir: Path, results) -> None:
    files_dir = exam_dir / "files"
    for result in results:
        _write_result_files(files_dir, result)


def _write_result_files(files_dir: Path, result) -> None:
    for exam_file in result.files:
        target = files_dir / exam_file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(exam_file.content)


def run_grade(cfg: Config) -> int:
    workdir = Workdir(cfg.workdir_root)
    exam_dir = workdir.active()
    plan_payload = workdir.load_plan(exam_dir)
    plan = _plan_from_payload(plan_payload)
    results = render_exam(plan)

    env = MinikubeEnv(profile=cfg.minikube_profile)
    if not env.status().get("running"):
        raise RuntimeError(
            f"exam cluster '{cfg.minikube_profile}' is not running. Run `cka-mock new` first."
        )
    kubectl = env.kubectl(exam_dir / "kubeconfig")
    grade = grade_exam(results, kubectl.run)
    report = render_text_report(grade)
    workdir.write(exam_dir, "report.txt", report)
    record_grade(cfg.workdir_root, exam_dir.name, grade.fraction, grade.passed_checks, grade.total_checks)
    console.print(report)
    return 0


def run_reset(cfg: Config) -> int:
    env = MinikubeEnv(profile=cfg.minikube_profile)
    console.print(f"Deleting minikube profile {cfg.minikube_profile} ...")
    env.delete()
    console.print("Reset complete. Exam history is kept (use `cka-mock list`); "
                  "remove old exams manually under the workdir if desired.")
    return 0


def run_status(cfg: Config) -> int:
    workdir = Workdir(cfg.workdir_root)
    try:
        exam_dir = workdir.active()
    except Exception as exc:
        console.print(str(exc))
        return 1
    payload = workdir.load_plan(exam_dir)
    created = exam_dir.name
    console.print(f"Exam       : {created}")
    console.print(f"Questions  : {len(payload['questions'])}")
    console.print(f"Exam dir   : {exam_dir}")
    console.print(f"Kubeconfig : {exam_dir / 'kubeconfig'}")
    status = MinikubeEnv(profile=cfg.minikube_profile).status()
    console.print(f"Cluster    : {'running' if status.get('running') else 'not running'}")
    return 0


def run_list(cfg: Config, json_output: bool = False) -> int:
    workdir = Workdir(cfg.workdir_root)
    entries = []
    for exam_dir in workdir.list_exams():
        payload = workdir.load_plan(exam_dir)
        entries.append(
            {
                "exam_id": payload["exam_id"],
                "questions": len(payload["questions"]),
                "archetypes": sorted({q["archetype"] for q in payload["questions"]}),
                "created": payload["exam_id"].removeprefix("exam-"),
            }
        )
    if json_output:
        console.print(json.dumps(entries, indent=2))
    else:
        for entry in entries:
            console.print(f"{entry['exam_id']}  {entry['questions']}q  {', '.join(entry['archetypes'])}")
    return 0


def _plan_from_payload(payload: dict):
    from .schemas import ExamPlan, QuestionSpec

    return ExamPlan(
        questions=[QuestionSpec(archetype_id=q["archetype"], params=q["params"]) for q in payload["questions"]],
        meta=payload.get("meta", {}),
    )
