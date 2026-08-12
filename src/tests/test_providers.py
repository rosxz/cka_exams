from __future__ import annotations

import sys
import types

import pytest

from cka_mock.providers import (
    OpenCodeProvider,
    SequenceProvider,
    StaticProvider,
    _normalize_model,
    build_provider,
)


def test_normalize_model():
    assert _normalize_model("opencode/deepseek-v4-flash") == "deepseek-v4-flash"
    assert _normalize_model("openrouter/x") == "x"
    assert _normalize_model("deepseek-v4-flash") == "deepseek-v4-flash"
    assert _normalize_model("") == "deepseek-v4-flash"


def test_static_provider():
    p = StaticProvider("hello")
    assert p.generate("x").text == "hello"
    assert [piece.text for piece in p.generate_stream("x")] == ["hello"]


def test_sequence_provider():
    p = SequenceProvider(["a", "b"])
    assert p.generate("x").text == "a"
    assert p.generate("x").text == "b"
    assert p.generate("x").text == "b"  # clamps at last


def test_sequence_provider_rejects_empty():
    with pytest.raises(ValueError):
        SequenceProvider([])


def test_build_provider_invalid():
    with pytest.raises(ValueError):
        build_provider("unknown")


def test_opencode_defaults():
    p = OpenCodeProvider(api_key="k")
    assert p.model == "deepseek-v4-flash"
    assert p.base_url == "https://opencode.ai/zen/go/v1"
    kwargs = p._request_kwargs(stream=True)
    assert kwargs["stream"] is True
    assert kwargs["temperature"] == 0.2
    assert "reasoning_effort" not in kwargs


def test_opencode_reasoning_effort():
    p = OpenCodeProvider(api_key="k", reasoning_effort="off")
    assert p._request_kwargs(stream=False)["reasoning_effort"] == "off"


def test_opencode_requires_key(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = OpenCodeProvider()
    with pytest.raises(RuntimeError):
        p.generate("hi")


class _FakeChoice:
    def __init__(self, content: str):
        self.message = types.SimpleNamespace(content=content)


class _FakeCompletions:
    def __init__(self, text: str):
        self.text = text

    def create(self, **kwargs):
        return types.SimpleNamespace(choices=[_FakeChoice(self.text)])


class _FakeChat:
    def __init__(self, text: str):
        self.completions = _FakeCompletions(text)


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = _FakeChat("pong")


def test_opencode_generate(monkeypatch):
    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    p = OpenCodeProvider(api_key="test-key")
    res = p.generate("ping")
    assert res.text == "pong"
