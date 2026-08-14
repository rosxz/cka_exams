"""Downloaded upstream manifests used by install-yourself archetypes.

The pinned manifests (cert-manager, Contour) are fetched once at exam generation,
cached on disk, and written into the exam workdir as **local files** the candidate
applies — matching the real CKA, which has no internet and provides manifests as
local files. Nothing is vendored into the package; the cache makes repeat exams
instant and offline-capable.
"""
from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

import yaml


def cache_dir() -> Path:
    base = Path(os.environ.get("CKA_MOCK_HOME", "~/.cache/cka-mock")).expanduser()
    path = base / "manifests"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch_manifest(url: str, timeout: int = 60) -> str:
    """Download a pinned manifest, caching it on disk keyed by the URL."""
    key = hashlib.sha256(url.encode()).hexdigest()
    cached = cache_dir() / f"{key}.yaml"
    if cached.is_file():
        return cached.read_text(encoding="utf-8")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            content = response.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - report the URL and reason
        raise RuntimeError(f"could not download manifest from {url}: {exc}") from exc
    cached.write_text(content, encoding="utf-8")
    return content


def rewrite_contour_manifest(content: str, gateway_class: str) -> str:
    """Remove the install's default GatewayClass and point the served Gateway at
    the GatewayClass the candidate will create."""
    docs = [d for d in yaml.safe_load_all(content) if d]
    kept = []
    for doc in docs:
        if doc.get("kind") == "GatewayClass":
            continue
        if doc.get("kind") == "Gateway":
            doc["spec"]["gatewayClassName"] = gateway_class
        kept.append(doc)
    out = []
    for doc in kept:
        out.append("---\n")
        out.append(yaml.safe_dump(doc, sort_keys=False))
    return "".join(out)
