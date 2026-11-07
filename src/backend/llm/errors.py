"""Provider-neutral, redacted model error types."""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class ProviderErrorCode(str, Enum):
    CONFIGURATION = "configuration_error"
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION = "authentication_error"
    AUTHORIZATION = "authorization_error"
    QUOTA = "quota_error"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK = "network_error"
    TRANSIENT = "transient_provider_error"
    CONTENT_FILTER = "content_filter"
    INVALID_RESPONSE = "invalid_response"
    MODEL_MISMATCH = "model_mismatch"
    CIRCUIT_OPEN = "circuit_open"
    POLICY_LIMIT = "policy_limit"


def redact_text(
    value: str,
    *,
    secrets: Iterable[str] = (),
    max_length: int = 500,
) -> str:
    """Remove explicitly known secrets and cap diagnostic size."""

    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted[:max_length]


class ProviderError(RuntimeError):
    """Safe error raised by every model provider adapter."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        provider_request_id: str | None = None,
        retry_after_seconds: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        estimated_cost_cny: float = 0.0,
        secrets: Iterable[str] = (),
    ) -> None:
        if min(input_tokens, output_tokens, total_tokens) < 0:
            raise ValueError("Error token usage cannot be negative")
        if estimated_cost_cny < 0:
            raise ValueError("Error cost cannot be negative")
        self.code = code
        self.safe_message = redact_text(message, secrets=secrets)
        self.retryable = retryable
        self.http_status = http_status
        self.provider_request_id = provider_request_id
        self.retry_after_seconds = retry_after_seconds
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens
        self.estimated_cost_cny = estimated_cost_cny
        super().__init__(f"{code.value}: {self.safe_message}")


__all__ = ["ProviderError", "ProviderErrorCode", "redact_text"]
