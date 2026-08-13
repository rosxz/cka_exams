"""Configuration loading for cka_mock.

Config file ``cka-mock.toml`` is discovered from the current directory upward,
or passed explicitly via ``--config``. Environment variables override.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "cka-mock.toml"


def default_workdir_root() -> Path:
    return Path(os.environ.get("CKA_MOCK_HOME", "~/.cache/cka-mock")).expanduser()


@dataclass
class Config:
    provider: str = "opencode"
    model: str = "deepseek-v4-flash"
    base_url: str | None = None
    reasoning_effort: str | None = None
    minikube_profile: str = "cka-exam"
    minikube_cpus: int | None = None
    minikube_memory: int | None = None
    minikube_cni: str | None = "calico"
    questions: int = 17
    duration_minutes: int = 120
    exam_attempts: int = 3
    workdir_root: Path = field(default_factory=default_workdir_root)
    topics: list[str] = field(default_factory=list)
    addons: list[str] = field(default_factory=lambda: ["metrics-server"])
    difficulty: str = "medium"

    @property
    def api_key(self) -> str | None:
        return os.environ.get("OPENCODE_API_KEY") or os.environ.get("OPENAI_API_KEY")


def _find_config(start: Path | None = None) -> Path | None:
    here = Path(start) if start else Path.cwd()
    for parent in [here, *here.parents]:
        candidate = parent / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def _coerce(key: str, value):
    if key == "workdir_root":
        return Path(str(value)).expanduser()
    if key in (
        "minikube_cpus",
        "minikube_memory",
        "minikube_cni",
        "questions",
        "duration_minutes",
        "exam_attempts",
    ):
        return value if key == "minikube_cni" else int(value)
    return value


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from a file (or discover it) merged over defaults."""
    data: dict = {}
    candidate: Path | None = None
    if path is not None:
        candidate = Path(path)
        if not candidate.is_file():
            raise FileNotFoundError(f"Config file not found: {candidate}")
    else:
        candidate = _find_config()

    if candidate is not None:
        with open(candidate, "rb") as fh:
            data = tomllib.load(fh)

    kwargs: dict = {}
    allowed = {f.name for f in Config.__dataclass_fields__.values()}
    for key, value in data.items():
        if key in allowed:
            kwargs[key] = _coerce(key, value)
    return Config(**kwargs)
