"""Deterministic verifier engine.

Assertions are *data*: declarative descriptions of an expected cluster state.
The engine turns them into kubectl invocations and evaluates the results. No LLM
input ever reaches this path, so grading cannot be influenced by the model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AssertionResult:
    passed: bool
    description: str
    actual: Any = None


class Assertion:
    def to_argv(self) -> list[str]:
        raise NotImplementedError

    def evaluate(self, proc) -> AssertionResult:
        raise NotImplementedError


def _tokens(stdout: str) -> list[str]:
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except json.JSONDecodeError:
        pass
    return [token for token in stdout.split() if token]


def _compare(op: str, stdout: str, expect: Any, label: str) -> AssertionResult:
    actual: Any = stdout
    if op == "nonempty":
        passed = bool(stdout)
        return AssertionResult(passed, f"{label} is non-empty", actual=stdout)
    if op == "superset":
        wanted = {str(x) for x in expect}
        passed = wanted <= set(_tokens(stdout))
        return AssertionResult(passed, f"{label} covers all of {sorted(wanted)}", actual=_tokens(stdout))
    if op == "contains":
        passed = str(expect) in stdout
        return AssertionResult(passed, f"{label} contains {expect!r}", actual=stdout)
    if op == "gte":
        try:
            passed = int(stdout) >= int(expect)
            actual = int(stdout) if stdout else None
        except ValueError:
            passed = False
        return AssertionResult(passed, f"{label} >= {expect}", actual=actual)
    if op == "ne":
        return AssertionResult(not _eq(stdout, expect), f"{label} != {expect}", actual=stdout)

    return AssertionResult(_eq(stdout, expect), f"{label} == {expect}", actual=stdout)


def _eq(stdout: str, expect: Any) -> bool:
    if isinstance(expect, bool):
        return stdout == str(expect).lower()
    if isinstance(expect, (int, float)):
        try:
            return int(stdout) == int(expect)
        except ValueError:
            return False
    if isinstance(expect, dict):
        try:
            return json.loads(stdout) == expect
        except json.JSONDecodeError:
            return False
    if isinstance(expect, list):
        return {str(x) for x in expect} == set(_tokens(stdout))
    return stdout == str(expect)


@dataclass(frozen=True)
class ResourceAssertion(Assertion):
    resource: str
    name: str
    namespace: str | None = None
    jsonpath: str | None = None
    expect: Any = None
    op: str = "eq"  # eq | ne | gte | contains | superset | nonempty

    def to_argv(self) -> list[str]:
        argv = ["get", self.resource, self.name]
        if self.namespace:
            argv += ["-n", self.namespace]
        if self.jsonpath:
            argv += ["-o", f"jsonpath={self.jsonpath}"]
        return argv

    def evaluate(self, proc) -> AssertionResult:
        label = f"{self.resource}/{self.name}"
        if proc.returncode != 0:
            return AssertionResult(False, f"{label} exists", actual=proc.stderr.strip())
        if self.jsonpath is None:
            return AssertionResult(True, f"{label} exists")
        return _compare(self.op, proc.stdout.strip(), self.expect, f"{label}{self.jsonpath}")


@dataclass(frozen=True)
class CountAssertion(Assertion):
    resource: str
    namespace: str | None = None
    selector: str | None = None
    expect: int = 0
    op: str = "eq"  # eq | gte | lte

    def to_argv(self) -> list[str]:
        argv = ["get", self.resource, "--no-headers", "-o", "name"]
        if self.namespace:
            argv += ["-n", self.namespace]
        if self.selector:
            argv += ["-l", self.selector]
        return argv

    def evaluate(self, proc) -> AssertionResult:
        count = len([line for line in proc.stdout.splitlines() if line.strip()])
        if self.op == "gte":
            passed = count >= self.expect
        elif self.op == "lte":
            passed = count <= self.expect
        else:
            passed = count == self.expect
        return AssertionResult(
            passed,
            f"count of {self.resource}{' with ' + self.selector if self.selector else ''} {self.op} {self.expect}",
            actual=count,
        )


@dataclass(frozen=True)
class ExecAssertion(Assertion):
    pod: str
    namespace: str | None = None
    command: list[str] | None = None
    expect_rc: int = 0
    op: str = "eq"  # eq | ne

    def to_argv(self) -> list[str]:
        argv = ["exec"]
        if self.namespace:
            argv += ["-n", self.namespace]
        argv += [self.pod, "--", *(self.command or [])]
        return argv

    def evaluate(self, proc) -> AssertionResult:
        rc = proc.returncode
        passed = (rc == self.expect_rc) if self.op == "eq" else (rc != self.expect_rc)
        return AssertionResult(
            passed,
            f"exec {self.pod} -- {' '.join(self.command or [])} rc {self.op} {self.expect_rc}",
            actual=rc,
        )


@dataclass(frozen=True)
class ExecContentAssertion(Assertion):
    pod: str
    namespace: str | None = None
    command: list[str] | None = None
    expect_contains: str = ""

    def to_argv(self) -> list[str]:
        argv = ["exec"]
        if self.namespace:
            argv += ["-n", self.namespace]
        argv += [self.pod, "--", *(self.command or [])]
        return argv

    def evaluate(self, proc) -> AssertionResult:
        if proc.returncode != 0:
            return AssertionResult(
                False,
                f"exec {self.pod} -- {' '.join(self.command or [])} rc 0",
                actual=f"rc={proc.returncode}: {proc.stderr.strip()[:80]}",
            )
        passed = self.expect_contains in proc.stdout
        return AssertionResult(
            passed,
            f"exec {self.pod} -- {' '.join(self.command or [])} output contains {self.expect_contains!r}",
            actual=proc.stdout.strip()[:80],
        )


def run_assertions(assertions: list[Assertion], runner) -> list[AssertionResult]:
    """Run assertions against a kubectl runner callable.

    ``runner(argv)`` must return an object with ``returncode``, ``stdout``, and
    ``stderr`` attributes (e.g. ``Kubectl.run``).
    """
    results: list[AssertionResult] = []
    for assertion in assertions:
        proc = runner(assertion.to_argv())
        results.append(assertion.evaluate(proc))
    return results
