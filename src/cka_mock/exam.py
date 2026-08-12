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
from .generation import generate_exam_plan
from .history import add_fingerprints, load_fingerprints, record_grade
from .preflight import preflight_result
from .providers import build_provider
from .renderer import render_exam, render_task_markdown
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
    plan, raw = generate_exam_plan(
        provider,
        topics=topics,
        num_questions=questions,
        difficulty=difficulty,
        fingerprints=load_fingerprints(cfg.workdir_root),
    )
    add_fingerprints(cfg.workdir_root, [q.params | {"archetype": q.archetype_id} for q in plan.questions])
    results = render_exam(plan)

    workdir = Workdir(cfg.workdir_root)
    exam_dir = workdir.new_exam()

    env = MinikubeEnv(
        profile=cfg.minikube_profile,
        cpus=cfg.minikube_cpus,
        memory=cfg.minikube_memory,
        cni=cfg.minikube_cni,
        addons=tuple(cfg.addons),
    )
    console.print(f"[bold]Resetting minikube profile[/bold] {cfg.minikube_profile} ...")
    env.start(reset=True)

    kubeconfig = env.export_kubeconfig(exam_dir / "kubeconfig")
    kubectl = env.kubectl(kubeconfig)
    node_name = env.node_name(kubectl)

    workdir.save_plan(exam_dir, plan)
    workdir.write(exam_dir, "questions.md", render_task_markdown(results))
    workdir.write(exam_dir, "generation.json", raw)
    _write_exam_files(exam_dir, results)

    if any(r.archetype_id == "helm" for r in results) and not shutil.which("helm"):
        console.print("[yellow]warn[/yellow] this exam includes a Helm challenge but `helm` is not on PATH")

    console.print(f"[bold]Cluster ready[/bold] (node {node_name}). Setting up challenges...")
    preflight_warnings: list[str] = []
    for result in results:
        console.print(f"  Q{result.question_index}: {result.archetype_id} ...")
        skip_kinds = {"Deployment"} if result.archetype_id == "troubleshooting_crashloop" else None
        apply_result_setup(kubectl, result, node_name)
        setup_warnings = wait_for_manifests(kubectl, result.setup_manifests, skip_kinds=skip_kinds)
        for warning in setup_warnings:
            console.print(f"    [yellow]warn[/yellow] Q{result.question_index}: {warning}")
        report = preflight_result(kubectl, result, node_name)
        preflight_warnings.extend(report.warnings)
        for warning in report.warnings:
            console.print(f"    [yellow]preflight[/yellow] Q{result.question_index}: {warning}")

    workdir.write(exam_dir, "preflight.json", json.dumps({"warnings": preflight_warnings}, indent=2))

    console.print()
    console.print("[bold]Exam ready![/bold]")
    console.print(f"  Exam dir : {exam_dir}")
    console.print(f"  Questions: {len(results)}")
    console.print(f"  Duration : {cfg.duration_minutes} min")
    console.print(f"  Context  : {cfg.minikube_profile}")
    console.print()
    console.print("  Solve the questions in your terminal, e.g.:")
    console.print(f"    export KUBECONFIG={exam_dir / 'kubeconfig'}")
    console.print(f"    cat {exam_dir / 'questions.md'}")
    console.print(f"    ls {exam_dir / 'files'}   # provided files for file-based challenges")
    console.print("  When done, grade with:  cka-mock grade")
    return 0


def _write_exam_files(exam_dir: Path, results) -> None:
    files_dir = exam_dir / "files"
    for result in results:
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
    if cfg.workdir_root.exists():
        shutil.rmtree(cfg.workdir_root)
    console.print("Reset complete.")
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


def run_replay(cfg: Config, exam_id: str) -> int:
    workdir = Workdir(cfg.workdir_root)
    exam_dir = workdir.find(exam_id)
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
    console.print(report)
    return 0


def _plan_from_payload(payload: dict):
    from .schemas import ExamPlan, QuestionSpec

    return ExamPlan(
        questions=[QuestionSpec(archetype_id=q["archetype"], params=q["params"]) for q in payload["questions"]],
        meta=payload.get("meta", {}),
    )
