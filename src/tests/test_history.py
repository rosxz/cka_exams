from __future__ import annotations

import json

from cka_mock.history import add_fingerprints, fingerprint, load_fingerprints, record_grade


def test_fingerprint_is_order_independent():
    a = {"archetype": "deployment", "params": {"name": "x", "replicas": 3}}
    b = {"archetype": "deployment", "params": {"replicas": 3, "name": "x"}}
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint({"archetype": "deployment", "params": {"name": "y", "replicas": 3}})


def test_add_and_load_fingerprints(tmp_path):
    add_fingerprints(tmp_path, [{"archetype": "pvc", "params": {"name": "data"}}])
    add_fingerprints(tmp_path, [{"archetype": "pvc", "params": {"name": "data"}}])  # dedup
    fps = load_fingerprints(tmp_path)
    assert len(fps) == 1
    assert fps[0] == fingerprint({"archetype": "pvc", "params": {"name": "data"}})


def test_record_grade_journal(tmp_path):
    record_grade(tmp_path, "exam-1", 0.75, 3, 4)
    record_grade(tmp_path, "exam-2", 1.0, 4, 4)
    journal = json.loads((tmp_path / "journal.json").read_text())
    assert len(journal) == 2
    assert journal[-1]["exam_id"] == "exam-2"
    assert journal[-1]["fraction"] == 1.0
