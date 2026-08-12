"""Grading: run a rendered exam's assertions against the live cluster.

The grader is purely mechanical. It never asks the LLM whether an answer is
right — it executes declarative assertions and reports pass/fail per check.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .assertion import AssertionResult, run_assertions
from .renderer import RenderResult


@dataclass
class QuestionGrade:
    question_index: int
    archetype_id: str
    task: str
    results: list[AssertionResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def fraction(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass
class ExamGrade:
    questions: list[QuestionGrade] = field(default_factory=list)

    @property
    def total_checks(self) -> int:
        return sum(q.total for q in self.questions)

    @property
    def passed_checks(self) -> int:
        return sum(q.passed for q in self.questions)

    @property
    def fraction(self) -> float:
        return self.passed_checks / self.total_checks if self.total_checks else 0.0


def grade_exam(results: list[RenderResult], runner) -> ExamGrade:
    grade = ExamGrade()
    for rendered in results:
        grade.questions.append(
            QuestionGrade(
                question_index=rendered.question_index,
                archetype_id=rendered.archetype_id,
                task=rendered.task,
                results=run_assertions(rendered.assertions, runner),
            )
        )
    return grade
