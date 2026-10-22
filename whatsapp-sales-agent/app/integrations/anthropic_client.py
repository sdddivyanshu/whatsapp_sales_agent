"""LLM client interface with an Anthropic Claude implementation and a mock.

Structured output is obtained by forcing a single tool call whose
``input_schema`` is the expected JSON schema; the tool input *is* the result.
The interface is deliberately tiny so another provider can be dropped in.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    def structured(self, *, system: str, messages: list[dict[str, Any]], tool_name: str,
                   tool_description: str, schema: dict[str, Any],
                   context: dict[str, Any] | None = None) -> dict[str, Any]: ...


class AnthropicClient:
    def __init__(self, api_key: str, model: str, max_tokens: int = 1500,
                 timeout: float = 60.0, max_retries: int = 2) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError("The 'anthropic' package is required for LLM_MODE=live") from exc
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)
        self.model = model
        self.max_tokens = max_tokens

    def structured(self, *, system: str, messages: list[dict[str, Any]], tool_name: str,
                   tool_description: str, schema: dict[str, Any],
                   context: dict[str, Any] | None = None) -> dict[str, Any]:
        # ``context`` is already rendered into ``messages``; it is only used by the mock.
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages,
                tools=[{"name": tool_name, "description": tool_description, "input_schema": schema}],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except self._anthropic.APIError as exc:
            raise LLMError(f"Claude API error: {type(exc).__name__}: {exc}") from exc
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
                return dict(block.input)
        raise LLMError(f"Claude did not return the '{tool_name}' tool call "
                       f"(stop_reason={response.stop_reason})")


Handler = Callable[[str, list[dict[str, Any]], dict[str, Any]], dict[str, Any]]


class MockLLMClient:
    """Deterministic stand-in used in mock mode and tests.

    Handlers are registered per tool name and receive ``(system, messages, context)``.
    ``context`` is the structured data the caller rendered into the prompt, which
    lets fixture logic produce grounded, realistic replies offline.
    """

    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self.calls: list[dict[str, Any]] = []

    def register(self, tool_name: str, handler: Handler) -> None:
        self.handlers[tool_name] = handler

    def structured(self, *, system: str, messages: list[dict[str, Any]], tool_name: str,
                   tool_description: str, schema: dict[str, Any],
                   context: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append({"tool": tool_name, "system": system, "messages": messages,
                           "context": context})
        handler = self.handlers.get(tool_name)
        if handler is None:
            raise LLMError(f"No mock handler registered for {tool_name}")
        return handler(system, messages, context or {})
