from __future__ import annotations

import io
import json

import pytest

from cka_mock.manifests import fetch_manifest, rewrite_contour_manifest

_SAMPLE = """\
---
apiVersion: v1
kind: Namespace
metadata:
  name: projectcontour
---
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: example
spec:
  controllerName: projectcontour.io/gateway-controller
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: contour
  namespace: projectcontour
spec:
  gatewayClassName: example
"""


def test_fetch_manifest_downloads_and_caches(monkeypatch, tmp_path):
    import cka_mock.manifests as manifests

    monkeypatch.setattr(manifests, "cache_dir", lambda: tmp_path)
    calls = {"n": 0}

    class _FakeResponse:
        def read(self):
            return b"hello-manifest"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=60):
        calls["n"] += 1
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert fetch_manifest("https://example.com/x.yaml") == "hello-manifest"
    assert fetch_manifest("https://example.com/x.yaml") == "hello-manifest"
    assert calls["n"] == 1  # second call served from cache
    assert len(list(tmp_path.iterdir())) == 1


def test_fetch_manifest_failure_raises(monkeypatch, tmp_path):
    import cka_mock.manifests as manifests

    monkeypatch.setattr(manifests, "cache_dir", lambda: tmp_path)

    def fail(url, timeout=60):
        raise ConnectionError("network down")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(RuntimeError, match="could not download manifest"):
        fetch_manifest("https://example.com/x.yaml")


def test_rewrite_contour_manifest_strips_class_and_repoints_gateway():
    rewritten = rewrite_contour_manifest(_SAMPLE, "contour")
    import yaml

    docs = [d for d in yaml.safe_load_all(rewritten) if d]
    kinds = [d["kind"] for d in docs]
    assert "GatewayClass" not in kinds
    (gateway,) = [d for d in docs if d["kind"] == "Gateway"]
    assert gateway["spec"]["gatewayClassName"] == "contour"
