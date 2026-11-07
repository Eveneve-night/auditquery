"""Provider-neutral model request and response contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


ALLOWED_ROLES = {"system", "user", "assistant", "tool"}


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in ALLOWED_ROLES:
            raise ValueError(f"Unsupported message role: {self.role}")
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("Message content must be a non-empty string")


@dataclass(frozen=True)
class ModelCallRequest:
    request_id: str
    task_type: str
    messages: tuple[ModelMessage, ...]
    prompt_version: str
    output_schema_id: str
    max_completion_tokens: int
    temperature: float = 0.0
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("request_id is required")
        if not self.task_type.strip():
            raise ValueError("task_type is required")
        if not self.messages:
            raise ValueError("At least one model message is required")
        if not self.prompt_version.strip() or not self.output_schema_id.strip():
            raise ValueError("Prompt and output schema versions are required")
        if self.max_completion_tokens <= 0:
            raise ValueError("max_completion_tokens must be positive")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.total_tokens) < 0:
            raise ValueError("Token usage cannot be negative")
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens cannot be less than input + output")


@dataclass(frozen=True)
class ModelCallResult:
    request_id: str
    task_type: str
    provider_id: str
    requested_model: str
    returned_model: str
    provider_request_id: str | None
    parsed_output: dict[str, Any]
    usage: ModelUsage
    estimated_cost_cny: float
    latency_ms: float
    finish_reason: str | None
    attempt_count: int = 1
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.estimated_cost_cny < 0 or self.latency_ms < 0:
            raise ValueError("Cost and latency cannot be negative")
        if self.attempt_count <= 0:
            raise ValueError("attempt_count must be positive")
        object.__setattr__(self, "parsed_output", dict(self.parsed_output))


@runtime_checkable
class LLMProvider(Protocol):
    provider_id: str
    model: str

    def generate_structured(self, request: ModelCallRequest) -> ModelCallResult:
        """Generate and validate one JSON object response."""


__all__ = [
    "LLMProvider",
    "ModelCallRequest",
    "ModelCallResult",
    "ModelMessage",
    "ModelUsage",
]
