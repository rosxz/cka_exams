"""LLM provider abstraction for cka_mock.

OpenCode Go via the hosted OpenAI-compatible gateway is the default provider
(``https://opencode.ai/zen/go/v1``). Static/Sequence providers exist for tests
and deterministic replays.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterator, Protocol


@dataclass
class GenerationResult:
    text: str
    meta: dict[str, Any]


@dataclass
class StreamPiece:
    """A single streamed chunk of a model response.

    ``kind`` is either ``"thinking"`` (chain-of-thought) or ``"content"`` (the
    visible answer).
    """
    kind: str
    text: str


class Provider(Protocol):
    def generate(self, prompt: str) -> GenerationResult:
        ...

    def generate_stream(self, prompt: str) -> Iterator[StreamPiece]:
        ...

    def chat_stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[StreamPiece]:
        ...


def _normalize_model(model: str) -> str:
    """Strip a known provider prefix so bare model IDs are sent to the gateway."""
    normalized = model.strip()
    for prefix in ("opencode/", "openrouter/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return normalized or "deepseek-v4-flash"


class OpenCodeProvider:
    """OpenCode Go / OpenCode Zen via the hosted OpenAI-compatible gateway.

    Requires an API key from https://opencode.ai/auth. No local server needed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "deepseek-v4-flash",
        base_url: str | None = None,
        reasoning_effort: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("OPENCODE_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.model = _normalize_model(model)
        # OpenCode Go (subscription) uses its own gateway at /zen/go/v1.
        # The /zen/v1 path is the pay-as-you-go Zen gateway, which needs a separate balance.
        self.base_url = base_url or os.environ.get("OPENCODE_BASE_URL") or "https://opencode.ai/zen/go/v1"
        # e.g. "off", "low", "medium", "high", "max" (DeepSeek-family reasoning controls).
        self.reasoning_effort = reasoning_effort or os.environ.get("OPENCODE_REASONING_EFFORT")

    def _request_kwargs(self, *, stream: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": None,  # set by callers
            "temperature": 0.2,
        }
        if stream:
            kwargs["stream"] = True
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        return kwargs

    def _client(self):
        if not self.api_key:
            raise RuntimeError(
                "Missing OpenCode API key. Set OPENCODE_API_KEY or OPENAI_API_KEY, or pass --api-key."
            )

        try:
            import openai
        except Exception as exc:  # pragma: no cover - import error depends on environment
            raise RuntimeError(
                "openai is not installed. Install dependencies first."
            ) from exc

        self._openai = openai
        return openai.OpenAI(api_key=self.api_key, base_url=self.base_url)

    def _raise_helpful_error(self, exc: Exception) -> None:
        # Only wrap genuine connection failures. HTTP/API errors (401, 402, 429, ...)
        # carry the real reason and should surface as-is.
        api_connection_error = getattr(self._openai, "APIConnectionError", ())
        if not isinstance(exc, api_connection_error):
            raise exc

        raise RuntimeError(
            f"Could not connect to OpenCode gateway at {self.base_url}. "
            "For a hosted OpenCode Go/Zen API key, no local server is required. "
            "If you are using a custom/local endpoint, set OPENCODE_BASE_URL or pass --base-url."
        ) from exc

    @staticmethod
    def _rethrow_credits_error(exc: Exception) -> None:
        """Surface insufficient-balance / payment errors with a clear hint."""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        message = str(exc)
        if status in (401, 402, 403) or "credits" in message.lower() or "balance" in message.lower():
            raise RuntimeError(
                f"OpenCode gateway rejected the request: {message}\n"
                "If you are on the OpenCode Go subscription, make sure you are hitting the Go "
                "gateway (https://opencode.ai/zen/go/v1) and using a Go-covered model such as "
                "deepseek-v4-flash. The /zen/v1 path is the pay-as-you-go Zen gateway and "
                "requires a separate balance."
            ) from exc

    def generate(self, prompt: str) -> GenerationResult:
        client = self._client()
        kwargs = self._request_kwargs(stream=False)
        kwargs["messages"] = [{"role": "user", "content": prompt}]
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:
            self._rethrow_credits_error(exc)
            self._raise_helpful_error(exc)
        message = response.choices[0].message
        text = message.content or getattr(message, "reasoning_content", None) or ""
        meta = {}
        usage = getattr(response, "usage", None)
        if usage is not None:
            meta["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        return GenerationResult(text=text.strip(), meta=meta)

    def _yield_stream(self, stream) -> Iterator[StreamPiece]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        for event in stream:
            delta = event.choices[0].delta
            content = getattr(delta, "content", None)
            reasoning = getattr(delta, "reasoning_content", None)
            if content:
                content_parts.append(content)
                yield StreamPiece(kind="content", text=content)
            elif reasoning:
                reasoning_parts.append(reasoning)
                yield StreamPiece(kind="thinking", text=reasoning)

        # Go gateway quirk: when thinking is off, ALL text arrives in
        # `reasoning_content` and `content` stays empty. Treat it as the answer.
        if not content_parts and reasoning_parts:
            for part in reasoning_parts:
                yield StreamPiece(kind="content", text=part)

    def generate_stream(self, prompt: str) -> Iterator[StreamPiece]:
        client = self._client()
        kwargs = self._request_kwargs(stream=True)
        kwargs["messages"] = [{"role": "user", "content": prompt}]
        try:
            stream = client.chat.completions.create(**kwargs)
        except Exception as exc:
            self._rethrow_credits_error(exc)
            self._raise_helpful_error(exc)
        yield from self._yield_stream(stream)

    def chat_stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[StreamPiece]:
        client = self._client()
        kwargs = self._request_kwargs(stream=True)
        kwargs["messages"] = [{"role": "system", "content": system}, *messages]
        try:
            stream = client.chat.completions.create(**kwargs)
        except Exception as exc:
            self._rethrow_credits_error(exc)
            self._raise_helpful_error(exc)
        yield from self._yield_stream(stream)


class StaticProvider:
    """Provider used for tests and offline dry-runs."""

    def __init__(self, response: str):
        self.response = response

    def generate(self, prompt: str) -> GenerationResult:
        return GenerationResult(text=self.response, meta={"prompt": prompt})

    def generate_stream(self, prompt: str) -> Iterator[StreamPiece]:
        yield StreamPiece(kind="content", text=self.response)

    def chat_stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[StreamPiece]:
        yield StreamPiece(kind="content", text=self.response)


class SequenceProvider:
    """Provider that returns a different response each call.

    Useful for tests that need to simulate retries.
    """

    def __init__(self, responses: list[str]):
        if not responses:
            raise ValueError("responses must not be empty")
        self.responses = responses
        self.index = 0

    def generate(self, prompt: str) -> GenerationResult:
        response = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return GenerationResult(text=response, meta={"prompt": prompt, "call_index": self.index})

    def generate_stream(self, prompt: str) -> Iterator[StreamPiece]:
        yield StreamPiece(kind="content", text=self.responses[min(self.index, len(self.responses) - 1)])
        self.index += 1

    def chat_stream(self, system: str, messages: list[dict[str, str]]) -> Iterator[StreamPiece]:
        yield StreamPiece(kind="content", text=self.responses[min(self.index, len(self.responses) - 1)])
        self.index += 1


def build_provider(
    name: str = "opencode",
    api_key: str | None = None,
    model: str = "deepseek-v4-flash",
    base_url: str | None = None,
    reasoning_effort: str | None = None,
) -> Provider:
    if name == "opencode":
        return OpenCodeProvider(
            api_key=api_key, model=model, base_url=base_url, reasoning_effort=reasoning_effort
        )
    raise ValueError(f"Unsupported provider: {name}")
