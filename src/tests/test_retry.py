from __future__ import annotations

import types

import pytest

from cka_mock.config import Config
from cka_mock.exam import run_new, _setup_questions
from cka_mock.preflight import PreflightError
from cka_mock.schemas import ExamPlan, QuestionSpec

_PLAN = ExamPlan(
    questions=[
        QuestionSpec(
            "deployment",
            {
                "name": "web",
                "namespace": "app",
                "image": "nginx:1.27",
                "replicas": 1,
                "labels": {"app": "web"},
            },
        )
    ]
)


def test_run_new_retries_and_regenerates(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENCODE_API_KEY", "k")
    import cka_mock.exam as exam_mod

    captured = {"flow_calls": 0, "rejected_on_attempt2": None}

    def fake_plan(provider, *, topics, num_questions, difficulty, fingerprints, rejected, max_per_family):
        captured["rejected_on_attempt2"] = rejected
        return _PLAN, "{}"

    def fake_flow(cfg, plan, results, workdir, exam_dir, *, raw, provider=None):
        captured["flow_calls"] += 1
        if captured["flow_calls"] == 1:
            raise RuntimeError("preflight failed: reference does not satisfy")
        workdir.save_plan(exam_dir, plan)
        return 0

    monkeypatch.setattr(exam_mod, "generate_exam_plan", fake_plan)
    monkeypatch.setattr(exam_mod, "_run_exam_flow", fake_flow)

    cfg = Config(workdir_root=tmp_path, exam_attempts=3)
    rc = run_new(cfg, topics_override=None, questions_override=None, difficulty_override=None)
    assert rc == 0
    assert captured["flow_calls"] == 2
    # second attempt was told which questions were rejected
    assert captured["rejected_on_attempt2"] is not None
    assert captured["rejected_on_attempt2"][0]["archetype"] == "deployment"
    assert "Regenerating exam" in capsys.readouterr().out


def test_run_new_gives_up_after_max_attempts(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENCODE_API_KEY", "k")
    import cka_mock.exam as exam_mod

    def fake_plan(provider, *, topics, num_questions, difficulty, fingerprints, rejected, max_per_family):
        return _PLAN, "{}"

    def fake_flow(cfg, plan, results, workdir, exam_dir, *, raw, provider=None):
        raise RuntimeError("preflight failed: reference does not satisfy")

    monkeypatch.setattr(exam_mod, "generate_exam_plan", fake_plan)
    monkeypatch.setattr(exam_mod, "_run_exam_flow", fake_flow)

    cfg = Config(workdir_root=tmp_path, exam_attempts=3)
    with pytest.raises(RuntimeError, match="preflight failed"):
        run_new(cfg, topics_override=None, questions_override=None, difficulty_override=None)
    out = capsys.readouterr().out
    assert out.count("Exam attempt") == 3
    # no half-baked exam dirs left behind
    assert not any(tmp_path.iterdir()) or all(
        not d.name.startswith("exam-") for d in tmp_path.iterdir()
    )


def test_setup_questions_repairs_failing_question_in_place(monkeypatch, tmp_path, capsys):
    """A preflight failure repairs only that question; the rest of the exam is kept."""
    import cka_mock.exam as exam_mod
    from cka_mock.renderer import render_exam

    plan = ExamPlan(
        questions=[
            QuestionSpec(
                "deployment",
                {
                    "name": "web",
                    "namespace": "app",
                    "image": "nginx:1.27",
                    "replicas": 1,
                    "labels": {"app": "web"},
                },
            ),
            QuestionSpec(
                "pvc",
                {
                    "name": "data",
                    "namespace": "db",
                    "access_mode": "ReadWriteOnce",
                    "size": "100Mi",
                    "storage_class": "slow",
                },
            ),
        ]
    )
    results = render_exam(plan)

    replacement = QuestionSpec(
        "service",
        {
            "name": "svc-replacement",
            "namespace": "svcns",
            "service_type": "ClusterIP",
            "port": 80,
            "target_port": 80,
            "backend_labels": {"app": "backend"},
        },
    )
    monkeypatch.setattr(exam_mod, "_repair_question", lambda *a, **k: replacement)
    monkeypatch.setattr(exam_mod, "apply_result_setup", lambda *a, **k: None)
    monkeypatch.setattr(exam_mod, "wait_for_manifests", lambda *a, **k: [])

    calls = {"preflight": 0}

    def fake_preflight(kubectl, result, node_name, **kwargs):
        calls["preflight"] += 1
        if result.question_index == 1 and calls["preflight"] == 1:
            raise PreflightError("Q1: reference does not satisfy")
        return types.SimpleNamespace(warnings=[])

    monkeypatch.setattr(exam_mod, "preflight_result", fake_preflight)

    log_lines: list[str] = []
    warnings, repaired = _setup_questions(
        Config(repair_attempts=3, max_per_family=3),
        plan,
        results,
        provider=object(),
        kubectl=object(),
        node_name="minikube",
        log=log_lines.append,
    )
    assert repaired == [0]
    # Q1 was replaced in place; Q2 untouched
    assert plan.questions[0].archetype_id == "service"
    assert plan.questions[1].archetype_id == "pvc"
    assert results[0].archetype_id == "service"
    assert results[0].question_index == 1
    assert any("regenerating just this challenge" in line for line in log_lines)


def test_setup_questions_gives_up_after_repair_attempts(monkeypatch):
    import cka_mock.exam as exam_mod
    from cka_mock.renderer import render_exam

    plan = ExamPlan(
        questions=[
            QuestionSpec(
                "deployment",
                {
                    "name": "web",
                    "namespace": "app",
                    "image": "nginx:1.27",
                    "replicas": 1,
                    "labels": {"app": "web"},
                },
            )
        ]
    )
    results = render_exam(plan)

    monkeypatch.setattr(exam_mod, "apply_result_setup", lambda *a, **k: None)
    monkeypatch.setattr(exam_mod, "wait_for_manifests", lambda *a, **k: [])

    def failing_preflight(kubectl, result, node_name, **kwargs):
        raise PreflightError("always fails")

    monkeypatch.setattr(exam_mod, "preflight_result", failing_preflight)

    with pytest.raises(PreflightError):
        _setup_questions(
            Config(repair_attempts=1, max_per_family=3),
            plan,
            results,
            provider=object(),
            kubectl=object(),
            node_name="minikube",
            log=lambda _m: None,
        )
