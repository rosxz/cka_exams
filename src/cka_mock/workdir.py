"""Per-exam working directory management.

Each exam lives in ``<root>/exam-<timestamp>/`` holding the exported kubeconfig,
the exam plan JSON, the rendered question sheet, and any provided files. A fresh
directory is created for every ``new`` and removed by ``reset``.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class ExamError(RuntimeError):
    pass


@dataclass
class Workdir:
    root: Path

    def new_exam(self) -> Path:
        exam_id = time.strftime("exam-%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        path = self.root / exam_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def is_exam_dir(path: Path) -> bool:
        return path.is_dir() and (path / "exam.json").is_file()

    def list_exams(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        exams = [p for p in self.root.iterdir() if self.is_exam_dir(p)]
        return sorted(exams, key=lambda p: self._created(p), reverse=True)

    @staticmethod
    def _created(path: Path) -> float:
        try:
            payload = json.loads((path / "exam.json").read_text())
            return float(payload.get("created", 0.0))
        except (OSError, json.JSONDecodeError, ValueError):
            return 0.0

    def active(self) -> Path:
        exams = self.list_exams()
        if not exams:
            raise ExamError(
                f"no exam found under {self.root}. Run `cka-mock new` first."
            )
        return exams[0]

    def find(self, exam_id: str) -> Path:
        path = self.root / exam_id
        if not self.is_exam_dir(path):
            raise ExamError(f"exam {exam_id!r} not found under {self.root}")
        return path

    def save_plan(self, path: Path, plan) -> None:
        payload = {
            "exam_id": path.name,
            "created": time.time(),
            "questions": [
                {"archetype": q.archetype_id, "params": q.params}
                for q in plan.questions
            ],
            "meta": plan.meta,
        }
        (path / "exam.json").write_text(json.dumps(payload, indent=2))

    def load_plan(self, path: Path) -> dict:
        return json.loads((path / "exam.json").read_text())

    def write(self, path: Path, name: str, content: str) -> Path:
        target = path / name
        target.write_text(content)
        return target
