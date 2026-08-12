"""Thin kubectl wrapper that pins context/kubeconfig for every invocation.

Every command the tool issues goes through this class so nothing can ever talk
to a cluster other than the dedicated exam cluster.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class Kubectl:
    def __init__(self, context: str | None = None, kubeconfig: str | Path | None = None):
        self.context = context
        self.kubeconfig = str(kubeconfig) if kubeconfig else None

    def run(
        self,
        argv: list[str],
        check: bool = False,
        timeout: int = 120,
        input: str | None = None,
    ) -> subprocess.CompletedProcess:
        full: list[str] = ["kubectl"]
        if self.kubeconfig:
            full += ["--kubeconfig", self.kubeconfig]
        if self.context:
            full += ["--context", self.context]
        full += argv
        return subprocess.run(
            full,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
            input=input,
        )
