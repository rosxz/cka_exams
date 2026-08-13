from __future__ import annotations

from cka_mock.config import Config
from cka_mock.exam import _cleanup_question_setup, _needed_addons
from cka_mock.renderer import RENDERERS, render_exam
from cka_mock.schemas import ExamPlan, QuestionSpec


def test_needed_addons_adds_ingress_when_present():
    plan = ExamPlan(
        questions=[
            QuestionSpec(
                "ingress",
                {
                    "name": "web-ing",
                    "namespace": "app",
                    "host": "web.example.com",
                    "marker": "m",
                    "labels": {"app": "web"},
                },
            ),
            QuestionSpec(
                "deployment",
                {
                    "name": "web",
                    "namespace": "app",
                    "image": "nginx:1.27",
                    "replicas": 1,
                    "labels": {"app": "web"},
                },
            ),
        ]
    )
    results = render_exam(plan)
    addons = _needed_addons(Config(addons=["metrics-server"]), results)
    assert "ingress" in addons
    assert "metrics-server" in addons


def test_needed_addons_without_ingress():
    plan = ExamPlan(
        questions=[
            QuestionSpec(
                "deployment",
                {
                    "name": "web",
                    "namespace": "app",
                    "image": "nginx:1.27",
                    "replicas": 1,
                    "labels": {"app": "web"},
                },
            )
        ]
    )
    results = render_exam(plan)
    assert "ingress" not in _needed_addons(Config(addons=["metrics-server"]), results)


def test_ingress_multi_included_in_ingress_family():
    plan = ExamPlan(
        questions=[
            QuestionSpec(
                "ingress_multi",
                {
                    "name": "shop-ing",
                    "namespace": "shop",
                    "host_a": "a.example.com",
                    "host_b": "b.example.com",
                    "marker_a": "a",
                    "marker_b": "b",
                    "labels_a": {"app": "a"},
                    "labels_b": {"app": "b"},
                },
            )
        ]
    )
    results = render_exam(plan)
    addons = _needed_addons(Config(addons=[]), results)
    assert "ingress" in addons


def test_cleanup_question_setup_skips_namespace(fake_tooling):
    from cka_mock.kubectl import Kubectl

    fake_tooling.respond("kubectl", "delete", "-f", "-", "--ignore-not-found")
    result = RENDERERS["service"](QuestionSpec(
        "service",
        {
            "name": "web-svc",
            "namespace": "frontend",
            "service_type": "ClusterIP",
            "port": 80,
            "target_port": 8080,
            "backend_labels": {"app": "web"},
        },
    ))
    _cleanup_question_setup(Kubectl(), result)
    delete_calls = [call for call in fake_tooling.calls if call[:2] == ["kubectl", "delete"]]
    # namespace is left in place; only the backend Deployment is removed
    assert len(delete_calls) == 1
