"""Exam history: fingerprints for anti-repeat and a score journal.

Fingerprints let the generator avoid reusing near-identical parameter sets across
exams; the journal records every graded exam so the next generation prompt and
the user can see progress over time.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


def fingerprint(question: dict) -> str:
    """Stable hash of an archetype + its params (order-independent)."""
    payload = json.dumps(question, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_fingerprints(root: Path) -> list[str]:
    path = root / "fingerprints.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def add_fingerprints(root: Path, questions: list[dict]) -> None:
    current = load_fingerprints(root)
    for question in questions:
        fp = fingerprint(question)
        if fp not in current:
            current.append(fp)
    root.mkdir(parents=True, exist_ok=True)
    (root / "fingerprints.json").write_text(json.dumps(current, indent=2))


def record_grade(root: Path, exam_id: str, fraction: float, passed: int, total: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "journal.json"
    try:
        journal = json.loads(path.read_text()) if path.is_file() else []
        if not isinstance(journal, list):
            journal = []
    except (OSError, json.JSONDecodeError):
        journal = []
    journal.append(
        {
            "exam_id": exam_id,
            "graded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "fraction": fraction,
            "passed": passed,
            "total": total,
        }
    )
    path.write_text(json.dumps(journal, indent=2))
