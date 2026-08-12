from __future__ import annotations

import pytest

from cka_mock.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "cka-mock" in out


def test_no_args_prints_help(capsys):
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage:" in out


def test_new_delegates_to_run_new(monkeypatch, capsys):
    captured = {}

    def fake_run_new(cfg, *, topics_override, questions_override, difficulty_override):
        captured["topics"] = topics_override
        captured["questions"] = questions_override
        captured["difficulty"] = difficulty_override
        captured["model"] = cfg.model
        return 0

    monkeypatch.setenv("OPENCODE_API_KEY", "k")
    monkeypatch.setattr("cka_mock.exam.run_new", fake_run_new)
    rc = main(["new", "--topics", "RBAC", "Helm", "--questions", "3", "--difficulty", "hard"])
    assert rc == 0
    assert captured["topics"] == ["RBAC", "Helm"]
    assert captured["questions"] == 3
    assert captured["difficulty"] == "hard"
    assert captured["model"] == "deepseek-v4-flash"


def test_new_fails_without_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = main(["new"])
    assert rc == 1
    assert "OPENCODE_API_KEY" in capsys.readouterr().out


def test_grade_without_exam_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CKA_MOCK_HOME", str(tmp_path))
    rc = main(["grade"])
    assert rc == 1
    assert "no exam found" in capsys.readouterr().out


def test_list_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CKA_MOCK_HOME", str(tmp_path))
    rc = main(["list"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""
