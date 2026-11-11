"""The provider-neutral interface every model adapter implements (decision 039).

A request carries only text, a JSON schema and limits. There is deliberately no field for tools or
function declarations, so no adapter can send any (threat model T4).
"""

from dataclasses import dataclass
from typing import Any, Protocol

RATE_LIMITED = "429"
SERVER_ERROR = "5xx"


@dataclass(frozen=True)
class ModelRequest:
    model: str
    system: str
    user_text: str
    json_schema: dict[str, Any]
    max_output_tokens: int
    temperature: float = 0.0


@dataclass(frozen=True)
class ModelResponse:
    text: str
    input_tokens: int
    output_tokens: int
    finish_complete: bool = True


class Provider(Protocol):
    name: str

    def generate(self, request: ModelRequest) -> ModelResponse: ...


class LLMError(Exception):
    """Base of every error the LLM layer raises. Messages never hold the key or unmasked text."""


class ProviderError(LLMError):
    """The provider refused the request or answered in a way retrying cannot fix."""


class RetryableProviderError(ProviderError):
    """A 429, a 5xx, a timeout or a network error: worth another attempt."""

    def __init__(self, message: str, kind: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
