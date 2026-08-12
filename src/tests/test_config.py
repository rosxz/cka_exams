from __future__ import annotations

from pathlib import Path

import pytest

from cka_mock.config import load_config


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.provider == "opencode"
    assert cfg.model == "deepseek-v4-flash"
    assert cfg.minikube_profile == "cka-exam"
    assert cfg.questions == 17
    assert cfg.duration_minutes == 120
    assert cfg.topics == []
    assert "ingress" in cfg.addons


def test_load_from_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cka-mock.toml").write_text(
        'model = "my-model"\n'
        "questions = 10\n"
        'workdir_root = "/tmp/ckax"\n'
        'topics = ["RBAC", "Helm"]\n'
        'difficulty = "hard"\n'
    )
    cfg = load_config()
    assert cfg.model == "my-model"
    assert cfg.questions == 10
    assert cfg.workdir_root == Path("/tmp/ckax")
    assert cfg.topics == ["RBAC", "Helm"]
    assert cfg.difficulty == "hard"


def test_discovered_from_parent(tmp_path, monkeypatch):
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    (tmp_path / "cka-mock.toml").write_text("questions = 5\n")
    monkeypatch.chdir(child)
    cfg = load_config()
    assert cfg.questions == 5


def test_explicit_path(tmp_path):
    cfg_file = tmp_path / "custom.toml"
    cfg_file.write_text("questions = 3\n")
    cfg = load_config(cfg_file)
    assert cfg.questions == 3


def test_missing_explicit_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "secret")
    cfg = load_config()
    assert cfg.api_key == "secret"
