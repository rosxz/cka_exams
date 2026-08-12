"""Shared fixtures for cka_mock tests."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable

import pytest


@dataclass
class Canned:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeTooling:
    """Intercepts ``subprocess.run`` for kubectl/minikube/helm/kustomize.

    Register exact command responses with :meth:`respond` or dynamic handlers
    with :meth:`when`. Any unhandled command fails the test loudly.
    """

    def __init__(self) -> None:
        self.responses: dict[tuple[str, ...], Canned] = {}
        self.handlers: list[tuple[tuple[str, ...], Callable[[list[str]], "subprocess.CompletedProcess"]]] = []
        self.calls: list[list[str]] = []

    def respond(self, *argv: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.responses[tuple(argv)] = Canned(returncode, stdout, stderr)

    def when(self, *prefix: str):
        def deco(fn):
            self.handlers.append((prefix, fn))
            return fn

        return deco

    def run(self, argv, *args: Any, check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        result: subprocess.CompletedProcess | None = None

        for prefix, fn in self.handlers:
            if len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == prefix:
                result = fn(list(argv))
                break

        if result is None:
            key = tuple(argv)
            if key in self.responses:
                canned = self.responses[key]
                result = subprocess.CompletedProcess(argv, canned.returncode, canned.stdout, canned.stderr)

        if result is None:
            raise AssertionError(f"Unhandled command: {argv}")

        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, argv, output=result.stdout, stderr=result.stderr
            )
        return result


@pytest.fixture
def fake_tooling(monkeypatch):
    """Patches ``subprocess.run`` so tools can be scripted without a cluster."""
    ft = FakeTooling()
    monkeypatch.setattr(subprocess, "run", ft.run)
    return ft


@pytest.fixture
def fake_which(monkeypatch):
    def make_which(paths: dict[str, str]):
        def fake_which(name: str, **kwargs: Any) -> str | None:
            return paths.get(name)

        monkeypatch.setattr("shutil.which", fake_which)
        return fake_which

    return make_which
