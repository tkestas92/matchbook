"""A scripted model for tests: no network, no key, and a record of every request it received."""

from collections.abc import Iterable

from app.llm.provider import ModelRequest, ModelResponse

Scripted = str | ModelResponse | Exception


class FakeProvider:
    name = "fake"

    def __init__(self, script: Iterable[Scripted], tokens: tuple[int, int] = (100, 50)) -> None:
        self._script = list(script)
        self._tokens = tokens
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("FakeProvider script is exhausted")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, ModelResponse):
            return step
        return ModelResponse(text=step, input_tokens=self._tokens[0], output_tokens=self._tokens[1])
