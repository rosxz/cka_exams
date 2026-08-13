from __future__ import annotations

from cka_mock.assertion import (
    CountAssertion,
    ExecAssertion,
    ExecContentAssertion,
    ResourceAssertion,
    run_assertions,
)


def _completed(returncode, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess(["kubectl"], returncode, stdout, stderr)


def test_resource_exists():
    a = ResourceAssertion("deployments.apps", "web", "ns")
    assert a.to_argv() == ["get", "deployments.apps", "web", "-n", "ns"]
    assert a.evaluate(_completed(0)).passed is True
    assert a.evaluate(_completed(1, stderr="not found")).passed is False


def test_resource_jsonpath_eq():
    a = ResourceAssertion("deployments.apps", "web", "ns", "{.spec.replicas}", 3)
    assert a.evaluate(_completed(0, stdout="3")).passed is True
    assert a.evaluate(_completed(0, stdout="2")).passed is False


def test_jsonpath_dict_eq():
    a = ResourceAssertion("deployments.apps", "web", "ns", "{.spec.selector.matchLabels}", {"app": "web"})
    assert a.evaluate(_completed(0, stdout='{"app":"web"}')).passed is True
    assert a.evaluate(_completed(0, stdout='{"app":"other"}')).passed is False


def test_superset_dict_checks_pod_labels():
    # The Deployment labels requirement targets the pod template labels. Missing
    # a required label must fail; extra labels must pass.
    a = ResourceAssertion(
        "deployments.apps", "web-front", "ns", "{.spec.template.metadata.labels}",
        {"app": "web-front", "tier": "frontend"}, "superset",
    )
    assert a.evaluate(_completed(0, stdout='{"app":"web-front","tier":"frontend"}')).passed is True
    assert a.evaluate(_completed(0, stdout='{"app":"web-front","tier":"frontend","extra":"x"}')).passed is True
    assert a.evaluate(_completed(0, stdout='{"app":"web-front"}')).passed is False
    assert a.evaluate(_completed(0, stdout='{"tier":"frontend"}')).passed is False


def test_jsonpath_gte():
    a = ResourceAssertion("deployments.apps", "web", "ns", "{.status.availableReplicas}", 3, "gte")
    assert a.evaluate(_completed(0, stdout="4")).passed is True
    assert a.evaluate(_completed(0, stdout="2")).passed is False
    assert a.evaluate(_completed(0, stdout="")).passed is False


def test_superset_tolerates_flat_tokens():
    a = ResourceAssertion("roles.rbac.authorization.k8s.io", "r", "ns", "{.rules[*].verbs[*]}", ["get", "list"], "superset")
    assert a.evaluate(_completed(0, stdout="get list watch")).passed is True
    assert a.evaluate(_completed(0, stdout="get")).passed is False


def test_superset_parses_json_list():
    a = ResourceAssertion("roles.rbac.authorization.k8s.io", "r", "ns", "{.rules[*].resources[*]}", ["pods"], "superset")
    assert a.evaluate(_completed(0, stdout='["pods","services"]')).passed is True


def test_nonempty():
    a = ResourceAssertion("endpoints", "svc", "ns", "{.subsets[0].addresses[*].ip}", op="nonempty")
    assert a.evaluate(_completed(0, stdout="10.0.0.5")).passed is True
    assert a.evaluate(_completed(0, stdout="")).passed is False


def test_contains():
    a = ResourceAssertion("rolebindings.rbac.authorization.k8s.io", "b", "ns", "{.subjects[*].name}", "svcacc", "contains")
    assert a.evaluate(_completed(0, stdout="svcacc other")).passed is True


def test_count_assertion():
    a = CountAssertion("pods", "ns", "app=web", 3)
    assert a.evaluate(_completed(0, stdout="p/1\np/2\np/3")).passed is True
    assert a.evaluate(_completed(0, stdout="p/1\np/2")).passed is False


def test_count_gte():
    a = CountAssertion("pods", "ns", "app=web", 3, "gte")
    assert a.evaluate(_completed(0, stdout="p/1\np/2\np/3\np/4")).passed is True


def test_exec_assertion():
    a = ExecAssertion("peer", "ns", ["wget", "http://svc:80/"], 0)
    assert a.evaluate(_completed(0)).passed is True
    assert a.evaluate(_completed(1)).passed is False

    denied = ExecAssertion("blocked", "ns", ["wget", "http://svc:80/"], 0, "ne")
    assert denied.evaluate(_completed(1)).passed is True
    assert denied.evaluate(_completed(0)).passed is False


def test_exec_content_assertion():
    a = ExecContentAssertion("probe", "ns", ["wget", "http://svc/"], expect_contains="hello")
    assert a.evaluate(_completed(0, stdout="page says hello world")).passed is True
    assert a.evaluate(_completed(0, stdout="page says bye")).passed is False
    assert a.evaluate(_completed(1, stderr="not found")).passed is False


def test_run_assertions_uses_runner(fake_tooling):
    fake_tooling.respond("kubectl", "get", "deployments.apps", "web", "-n", "ns", stdout="web")
    fake_tooling.respond(
        "kubectl", "get", "deployments.apps", "web", "-n", "ns", "-o", "jsonpath={.spec.replicas}", stdout="3"
    )
    from cka_mock.kubectl import Kubectl

    kubectl = Kubectl()  # context None so argv matches registered responses
    results = run_assertions(
        [
            ResourceAssertion("deployments.apps", "web", "ns"),
            ResourceAssertion("deployments.apps", "web", "ns", "{.spec.replicas}", 3),
        ],
        kubectl.run,
    )
    assert all(result.passed for result in results)
