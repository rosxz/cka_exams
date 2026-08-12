from __future__ import annotations

import subprocess

from cka_mock.grader import grade_exam
from cka_mock.renderer import RENDERERS, render_exam
from cka_mock.schemas import ExamPlan, QuestionSpec
from cka_mock.kubectl import Kubectl


def _deploy_spec(replicas=3, image="nginx:1.27", name="web", namespace="frontend"):
    return QuestionSpec(
        archetype_id="deployment",
        params={
            "name": name,
            "namespace": namespace,
            "image": image,
            "replicas": replicas,
            "labels": {"app": "web"},
            "container_port": 80,
        },
    )


def _deploy_handler(name, namespace, jsonpath_values):
    def handler(argv):
        try:
            oi = argv.index("-o")
            path = argv[oi + 1].split("jsonpath=", 1)[1].strip().strip("{}")
        except (ValueError, IndexError):
            return subprocess.CompletedProcess(argv, 0, f"{name}", "")
        value = jsonpath_values.get(path, "0")
        return subprocess.CompletedProcess(argv, 0, value, "")

    return handler


def test_grade_full_pass(fake_tooling):
    fake_tooling.when("kubectl", "get", "deployments.apps")(_deploy_handler("web", "frontend", {
        ".spec.replicas": "3",
        ".spec.template.spec.containers[0].image": "nginx:1.27",
        ".spec.selector.matchLabels": '{"app":"web"}',
        ".status.availableReplicas": "3",
    }))
    kubectl = Kubectl()
    plan = ExamPlan(questions=[_deploy_spec()])
    results = render_exam(plan)
    grade = grade_exam(results, kubectl.run)
    assert len(grade.questions) == 1
    q = grade.questions[0]
    assert q.passed == q.total
    assert grade.fraction == 1.0


def test_grade_partial_credit(fake_tooling):
    fake_tooling.when("kubectl", "get", "deployments.apps")(_deploy_handler("web", "frontend", {
        ".spec.replicas": "3",
        ".spec.template.spec.containers[0].image": "wrong-image",
        ".spec.selector.matchLabels": '{"app":"web"}',
        ".status.availableReplicas": "3",
    }))
    kubectl = Kubectl()
    plan = ExamPlan(questions=[_deploy_spec()])
    grade = grade_exam(render_exam(plan), kubectl.run)
    q = grade.questions[0]
    assert q.passed < q.total
    assert 0.0 < grade.fraction < 1.0
    assert any("wrong-image" in (r.description + str(r.actual)) for r in q.results if not r.passed)


def test_grade_multi_question(fake_tooling):
    def handler(name, namespace):
        return _deploy_handler(name, namespace, {
            ".spec.replicas": "2",
            ".spec.template.spec.containers[0].image": "redis:7.2",
            ".spec.selector.matchLabels": '{"app":"web"}',
            ".status.availableReplicas": "2",
        })

    fake_tooling.when("kubectl", "get", "deployments.apps")(lambda argv: handler(argv[3], argv[5])(argv))

    kubectl = Kubectl()
    plan = ExamPlan(
        questions=[
            _deploy_spec(replicas=2, image="redis:7.2", name="cache", namespace="cache"),
            _deploy_spec(replicas=2, image="redis:7.2", name="cache2", namespace="cache2"),
        ]
    )
    grade = grade_exam(render_exam(plan), kubectl.run)
    assert len(grade.questions) == 2
    assert grade.fraction == 1.0


def test_renderer_module_is_importable():
    import cka_mock.report  # noqa: F401
