from __future__ import annotations

import pytest

from cka_mock.config import Config
from cka_mock.exam import run_new
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

    def fake_plan(provider, *, topics, num_questions, difficulty, fingerprints, rejected):
        captured["rejected_on_attempt2"] = rejected
        return _PLAN, "{}"

    def fake_flow(cfg, plan, results, workdir, exam_dir, *, raw):
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

    def fake_plan(provider, *, topics, num_questions, difficulty, fingerprints, rejected):
        return _PLAN, "{}"

    def fake_flow(cfg, plan, results, workdir, exam_dir, *, raw):
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
