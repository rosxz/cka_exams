from __future__ import annotations

import json
import time

import pytest

from cka_mock.workdir import ExamError, Workdir
from cka_mock.schemas import ExamPlan, QuestionSpec


def _plan():
    return ExamPlan(
        questions=[QuestionSpec("deployment", {"name": "web", "namespace": "app"})],
        meta={"difficulty": "medium"},
    )


def test_new_exam_and_roundtrip(tmp_path):
    workdir = Workdir(tmp_path)
    path = workdir.new_exam()
    assert path.name.startswith("exam-")
    assert path.parent == tmp_path

    workdir.save_plan(path, _plan())
    payload = workdir.load_plan(path)
    assert payload["questions"][0]["archetype"] == "deployment"


def test_list_and_active_pick_newest(tmp_path):
    workdir = Workdir(tmp_path)
    first = workdir.new_exam()
    workdir.save_plan(first, _plan())
    time.sleep(0.01)
    second = workdir.new_exam()
    workdir.save_plan(second, _plan())

    exams = workdir.list_exams()
    assert [p.name for p in exams] == [second.name, first.name]
    assert workdir.active() == second


def test_active_raises_when_empty(tmp_path):
    workdir = Workdir(tmp_path)
    with pytest.raises(ExamError):
        workdir.active()


def test_find_existing_and_missing(tmp_path):
    workdir = Workdir(tmp_path)
    path = workdir.new_exam()
    workdir.save_plan(path, _plan())
    assert workdir.find(path.name) == path
    with pytest.raises(ExamError):
        workdir.find("exam-nope")


def test_write(tmp_path):
    workdir = Workdir(tmp_path)
    path = workdir.new_exam()
    target = workdir.write(path, "questions.md", "# hi")
    assert target.read_text() == "# hi"
