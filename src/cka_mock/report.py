"""Plain-text report rendering for exam grades."""
from __future__ import annotations

from .grader import ExamGrade


def render_text_report(grade: ExamGrade) -> str:
    lines: list[str] = []
    for q in grade.questions:
        status = "PASS" if q.passed == q.total else "FAIL"
        lines.append(f"Q{q.question_index} [{q.archetype_id}] {status} ({q.passed}/{q.total})")
        for result in q.results:
            marker = "  ok " if result.passed else "  XX "
            line = f"{marker}{result.description}"
            if not result.passed and result.actual is not None:
                line += f"   (got: {result.actual})"
            lines.append(line)
    lines.append("")
    lines.append(f"Score: {grade.fraction * 100:.1f}%  ({grade.passed_checks}/{grade.total_checks} checks)")
    return "\n".join(lines)
