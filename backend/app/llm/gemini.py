"""Google Gemini adapter over plain httpx (decision 039).

The request body is built here from an explicit dict: system instruction, one user turn and a
generation config. It never contains `tools` or `toolConfig` (threat model T4). The key travels in
the `x-goog-api-key` header, never in the URL, and no error message quotes headers or bodies (T6).
"""

import json
import re
from typing import Any

import httpx
from app.llm.provider import (
    RATE_LIMITED,
    SERVER_ERROR,
    ModelRequest,
    ModelResponse,
    ProviderError,
    RetryableProviderError,
)
from pydantic import SecretStr

COMPLETE_FINISH_REASONS = {"STOP"}
RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


def default_http_client(timeout_seconds: float) -> httpx.Client:
    """The only place a real network client is built. Tests replace this function."""
    return httpx.Client(timeout=httpx.Timeout(timeout_seconds))


class GeminiProvider:
    name = "gemini"

    def __init__(
        self, api_key: SecretStr, client: httpx.Client, base_url: str, max_response_bytes: int
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._max_response_bytes = max_response_bytes

    def generate(self, request: ModelRequest) -> ModelResponse:
        url = f"{self._base_url}/models/{request.model}:generateContent"
        headers = {"x-goog-api-key": self._api_key.get_secret_value()}
        try:
            with self._client.stream("POST", url, headers=headers, json=build_body(request)) as r:
                body = read_bounded(r, self._max_response_bytes)
                status = r.status_code
                header_retry_after = r.headers.get("Retry-After", "")
        except httpx.TimeoutException as exc:
            raise RetryableProviderError("Gemini request timed out", SERVER_ERROR) from exc
        except httpx.TransportError as exc:
            raise RetryableProviderError(
                f"Gemini network error: {type(exc).__name__}", SERVER_ERROR
            ) from exc
        if status == 429:
            raise RetryableProviderError(
                "Gemini rate limit (HTTP 429)",
                RATE_LIMITED,
                retry_after_seconds(header_retry_after, body),
            )
        if status >= 500:
            raise RetryableProviderError(f"Gemini server error (HTTP {status})", SERVER_ERROR)
        if status != 200:
            raise ProviderError(f"Gemini refused the request (HTTP {status})")
        return parse_response(body)


def build_body(request: ModelRequest) -> dict[str, Any]:
    return {
        "systemInstruction": {"parts": [{"text": request.system}]},
        "contents": [{"role": "user", "parts": [{"text": request.user_text}]}],
        "generationConfig": {
            "temperature": request.temperature,
            "maxOutputTokens": request.max_output_tokens,
            "responseMimeType": "application/json",
            "responseJsonSchema": request.json_schema,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }


def read_bounded(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise ProviderError(f"Gemini response is larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def retry_after_seconds(header: str, body: bytes) -> float:
    """The numeric Retry-After header, else the RetryInfo delay in the error body; 0 if neither."""
    if header.isdecimal():
        return float(header)
    try:
        details = json.loads(body)["error"]["details"]
    except (ValueError, KeyError, TypeError):
        return 0.0
    for detail in details if isinstance(details, list) else []:
        delay = detail.get("retryDelay") if isinstance(detail, dict) else None
        if isinstance(delay, str) and (match := RETRY_DELAY.match(delay)):
            return float(match.group(1))
    return 0.0


def parse_response(body: bytes) -> ModelResponse:
    try:
        payload = json.loads(body)
        usage = payload.get("usageMetadata", {})
        candidates = payload.get("candidates") or []
        candidate = candidates[0] if candidates else {}
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)
        input_tokens = int(usage.get("promptTokenCount", 0))
        output_tokens = int(usage.get("candidatesTokenCount", 0)) + int(
            usage.get("thoughtsTokenCount", 0)
        )
    except (ValueError, AttributeError, TypeError) as exc:
        raise ProviderError("Gemini returned a malformed response body") from exc
    return ModelResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_complete=candidate.get("finishReason") in COMPLETE_FINISH_REASONS,
    )
