"""Gemini adapter against httpx.MockTransport: the exact request we send and how answers map."""

import json
from collections.abc import Callable

import httpx
import pytest
from app.llm.gemini import GeminiProvider, retry_after_seconds
from app.llm.provider import (
    RATE_LIMITED,
    SERVER_ERROR,
    ModelRequest,
    ProviderError,
    RetryableProviderError,
)
from pydantic import SecretStr

KEY = "test-key-not-real-0000"
BASE_URL = "https://gemini.test/v1beta"
REQUEST = ModelRequest(
    model="gemini-2.5-flash-lite",
    system="Return JSON.",
    user_text="Invoice [IBAN_1] total 1.00",
    json_schema={"type": "object", "properties": {"total": {"type": "string"}}},
    max_output_tokens=256,
)
OK_BODY = {
    "candidates": [
        {
            "content": {"parts": [{"text": '{"total": '}, {"text": '"1.00"}'}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 7, "thoughtsTokenCount": 3},
}


def provider_for(
    handler: Callable[[httpx.Request], httpx.Response], max_bytes: int = 100_000
) -> GeminiProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GeminiProvider(SecretStr(KEY), client, BASE_URL, max_response_bytes=max_bytes)


def test_request_body_is_strict_json_with_no_tools() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    provider_for(handler).generate(REQUEST)
    request = seen[0]
    body = json.loads(request.content)
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/models/gemini-2.5-flash-lite:generateContent"
    assert set(body) == {"systemInstruction", "contents", "generationConfig"}
    raw = request.content.decode()
    for forbidden in ("tools", "toolConfig", "functionDeclarations", "functionCall"):
        assert forbidden not in raw
    config = body["generationConfig"]
    assert config["temperature"] == 0
    assert config["maxOutputTokens"] == 256
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == REQUEST.json_schema
    assert body["contents"] == [{"role": "user", "parts": [{"text": REQUEST.user_text}]}]
    assert body["systemInstruction"] == {"parts": [{"text": "Return JSON."}]}


def test_key_is_sent_in_a_header_never_in_the_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    provider_for(handler).generate(REQUEST)
    assert seen[0].headers["x-goog-api-key"] == KEY
    assert KEY not in str(seen[0].url)
    assert KEY not in seen[0].content.decode()


def test_answer_text_and_usage_are_parsed() -> None:
    response = provider_for(lambda _: httpx.Response(200, json=OK_BODY)).generate(REQUEST)
    assert response.text == '{"total": "1.00"}'
    assert response.input_tokens == 12
    assert response.output_tokens == 10  # thinking tokens are billed as output
    assert response.finish_complete


@pytest.mark.parametrize("reason", ["MAX_TOKENS", "SAFETY", None])
def test_unfinished_answer_is_not_complete(reason: str | None) -> None:
    body = {"candidates": [{"content": {"parts": [{"text": "{"}]}, "finishReason": reason}]}
    response = provider_for(lambda _: httpx.Response(200, json=body)).generate(REQUEST)
    assert not response.finish_complete


def test_blocked_prompt_without_candidates_is_not_complete() -> None:
    body = {"promptFeedback": {"blockReason": "SAFETY"}}
    response = provider_for(lambda _: httpx.Response(200, json=body)).generate(REQUEST)
    assert response.text == "" and not response.finish_complete


def test_429_is_retryable_with_retry_after() -> None:
    provider = provider_for(lambda _: httpx.Response(429, headers={"Retry-After": "12"}))
    with pytest.raises(RetryableProviderError) as caught:
        provider.generate(REQUEST)
    assert caught.value.kind == RATE_LIMITED
    assert caught.value.retry_after == 12


def test_429_retry_delay_from_the_error_body() -> None:
    body = {
        "error": {
            "code": 429,
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}],
        }
    }
    provider = provider_for(lambda _: httpx.Response(429, json=body))
    with pytest.raises(RetryableProviderError) as caught:
        provider.generate(REQUEST)
    assert caught.value.retry_after == 37


@pytest.mark.parametrize(
    ("header", "body", "expected"),
    [("5", b"", 5.0), ("", b"not json", 0.0), ("", b'{"error": {"details": "x"}}', 0.0)],
)
def test_retry_after_seconds(header: str, body: bytes, expected: float) -> None:
    assert retry_after_seconds(header, body) == expected


@pytest.mark.parametrize("status", [500, 503])
def test_server_errors_are_retryable(status: int) -> None:
    with pytest.raises(RetryableProviderError) as caught:
        provider_for(lambda _: httpx.Response(status)).generate(REQUEST)
    assert caught.value.kind == SERVER_ERROR


def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(RetryableProviderError) as caught:
        provider_for(handler).generate(REQUEST)
    assert caught.value.kind == SERVER_ERROR


def test_client_error_fails_without_echoing_key_or_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"bad request for key {KEY}")

    with pytest.raises(ProviderError) as caught:
        provider_for(handler).generate(REQUEST)
    assert not isinstance(caught.value, RetryableProviderError)
    assert KEY not in str(caught.value)
    assert "400" in str(caught.value)


def test_oversized_response_is_rejected() -> None:
    provider = provider_for(lambda _: httpx.Response(200, content=b"x" * 2000), max_bytes=1000)
    with pytest.raises(ProviderError, match="larger than 1000 bytes"):
        provider.generate(REQUEST)


def test_malformed_body_is_a_provider_error() -> None:
    with pytest.raises(ProviderError, match="malformed"):
        provider_for(lambda _: httpx.Response(200, content=b"<html>")).generate(REQUEST)
