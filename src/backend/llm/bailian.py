"""Strict qwen3.7-flash adapter for Alibaba Cloud Model Studio.

The adapter owns one HTTP attempt. Retry, circuit-breaker, and agent-loop
decisions belong to the reliability layer so failures cannot create hidden
loops here.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import socket
import time
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .contracts import ModelCallRequest, ModelCallResult, ModelUsage
from .credentials import CredentialError, CredentialProvider
from .errors import ProviderError, ProviderErrorCode, redact_text
from .usage import UsageLedger


APPROVED_MODEL = "qwen3.7-flash"
APPROVED_HOST = "dashscope.aliyuncs.com"
APPROVED_PATH = "/compatible-mode/v1"
APPROVED_PROTOCOL = "openai-compatible-chat-completions"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def send(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        """Send exactly one HTTP request."""


class UrllibHttpTransport:
    """Small standard-library transport with an explicit response-size cap."""

    @staticmethod
    def _read_limited(stream: Any, maximum: int) -> bytes:
        payload = stream.read(maximum + 1)
        if len(payload) > maximum:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "Provider response exceeded the configured byte limit",
                retryable=False,
            )
        return payload

    def send(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        request = Request(url=url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=self._read_limited(response, max_response_bytes),
                )
        except HTTPError as exc:
            return HttpResponse(
                status=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=self._read_limited(exc, max_response_bytes),
            )
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderError(
                ProviderErrorCode.TIMEOUT,
                "Provider request timed out",
                retryable=True,
            ) from exc
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise ProviderError(
                    ProviderErrorCode.TIMEOUT,
                    "Provider request timed out",
                    retryable=True,
                ) from exc
            raise ProviderError(
                ProviderErrorCode.NETWORK,
                "Provider network request failed",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise ProviderError(
                ProviderErrorCode.NETWORK,
                "Provider network request failed",
                retryable=True,
            ) from exc


@dataclass(frozen=True)
class BailianConfig:
    provider_id: str
    model: str
    endpoint: str
    timeout_seconds: float
    max_completion_tokens_cap: int
    max_response_bytes: int
    require_response_model_match: bool
    input_per_million_tokens: float
    output_per_million_tokens: float

    @classmethod
    def from_file(cls, path: str | Path) -> "BailianConfig":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            runtime = data["runtime"]
            pricing = data["pricing_snapshot"]
            base_url = str(data["base_url"]).rstrip("/")
            config = cls(
                provider_id=str(data["provider_id"]),
                model=str(data["model"]),
                endpoint=f"{base_url}/chat/completions",
                timeout_seconds=float(runtime["timeout_seconds"]),
                max_completion_tokens_cap=int(runtime["max_completion_tokens_cap"]),
                max_response_bytes=int(runtime["max_response_bytes"]),
                require_response_model_match=bool(
                    runtime["require_response_model_match"]
                ),
                input_per_million_tokens=float(
                    pricing["input_per_million_tokens"]
                ),
                output_per_million_tokens=float(
                    pricing["output_per_million_tokens"]
                ),
            )
        except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                ProviderErrorCode.CONFIGURATION,
                "Bailian provider configuration is missing or invalid",
                retryable=False,
            ) from exc

        parsed = urlparse(base_url)
        if data.get("protocol") != APPROVED_PROTOCOL:
            raise cls._configuration_error("Only the approved OpenAI-compatible protocol is allowed")
        if config.model != APPROVED_MODEL:
            raise cls._configuration_error("Only qwen3.7-flash is allowed")
        if parsed.scheme != "https" or parsed.hostname != APPROVED_HOST:
            raise cls._configuration_error("Bailian endpoint host is not approved")
        if parsed.path.rstrip("/") != APPROVED_PATH:
            raise cls._configuration_error("Bailian endpoint path is not approved")
        if (
            config.timeout_seconds <= 0
            or config.max_completion_tokens_cap <= 0
            or config.max_response_bytes <= 0
            or config.input_per_million_tokens < 0
            or config.output_per_million_tokens < 0
        ):
            raise cls._configuration_error("Bailian runtime or pricing limits are invalid")
        return config

    @staticmethod
    def _configuration_error(message: str) -> ProviderError:
        return ProviderError(
            ProviderErrorCode.CONFIGURATION, message, retryable=False
        )


class BailianProvider:
    """One-attempt, structured-output provider locked to qwen3.7-flash."""

    def __init__(
        self,
        config: BailianConfig,
        credential_provider: CredentialProvider,
        *,
        transport: HttpTransport | None = None,
        usage_ledger: UsageLedger | None = None,
    ) -> None:
        if config.model != APPROVED_MODEL:
            raise BailianConfig._configuration_error("Only qwen3.7-flash is allowed")
        self.config = config
        self.provider_id = config.provider_id
        self.model = config.model
        self.credential_provider = credential_provider
        self.transport = transport or UrllibHttpTransport()
        self.usage_ledger = usage_ledger or UsageLedger()

    def generate_structured(self, request: ModelCallRequest) -> ModelCallResult:
        started = time.monotonic()
        credential_value: str | None = None
        if request.max_completion_tokens > self.config.max_completion_tokens_cap:
            error = ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "Requested completion-token limit exceeds the configured cap",
                retryable=False,
            )
            self._record_failure(request, error, started)
            raise error

        try:
            credential = self.credential_provider.load()
            credential_value = credential.value
            payload = {
                "model": self.model,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
                "temperature": request.temperature,
                "max_completion_tokens": request.max_completion_tokens,
                "stream": False,
                "enable_thinking": False,
                "response_format": {"type": "json_object"},
            }
            response = self.transport.send(
                url=self.config.endpoint,
                headers={
                    "Authorization": f"Bearer {credential_value}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Client-Request-Id": request.request_id,
                },
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout_seconds=self.config.timeout_seconds,
                max_response_bytes=self.config.max_response_bytes,
            )
            provider_request_id = self._request_id(response.headers)
            if not 200 <= response.status < 300:
                raise self._http_error(
                    response,
                    provider_request_id=provider_request_id,
                    secret=credential_value,
                )
            result = self._parse_success(
                request,
                response,
                provider_request_id=provider_request_id,
                latency_ms=self._latency_ms(started),
                secret=credential_value,
            )
            self.usage_ledger.record_success(result)
            return result
        except CredentialError as exc:
            error = ProviderError(
                ProviderErrorCode.CONFIGURATION,
                "No valid API credential is available at the approved paths",
                retryable=False,
            )
            self._record_failure(request, error, started)
            raise error from exc
        except ProviderError as error:
            self._record_failure(request, error, started)
            raise
        except Exception as exc:
            error = ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "Unexpected provider adapter failure",
                retryable=False,
                secrets=(credential_value or "",),
            )
            self._record_failure(request, error, started)
            raise error from exc

    @staticmethod
    def _latency_ms(started: float) -> float:
        return max(0.0, (time.monotonic() - started) * 1000)

    def _record_failure(
        self, request: ModelCallRequest, error: ProviderError, started: float
    ) -> None:
        self.usage_ledger.record_failure(
            request_id=request.request_id,
            task_type=request.task_type,
            provider_id=self.provider_id,
            requested_model=self.model,
            error=error,
            latency_ms=self._latency_ms(started),
        )

    @staticmethod
    def _normalized_headers(headers: Mapping[str, str]) -> dict[str, str]:
        return {str(key).lower(): str(value) for key, value in headers.items()}

    @classmethod
    def _request_id(cls, headers: Mapping[str, str]) -> str | None:
        normalized = cls._normalized_headers(headers)
        for name in ("x-request-id", "request-id", "x-dashscope-request-id"):
            if normalized.get(name):
                return normalized[name]
        return None

    @staticmethod
    def _decode_object(body: bytes, *, secret: str) -> dict[str, Any]:
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "Provider returned invalid JSON",
                retryable=False,
                secrets=(secret,),
            ) from exc
        if not isinstance(decoded, dict):
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "Provider response must be a JSON object",
                retryable=False,
                secrets=(secret,),
            )
        return decoded

    def _parse_success(
        self,
        request: ModelCallRequest,
        response: HttpResponse,
        *,
        provider_request_id: str | None,
        latency_ms: float,
        secret: str,
    ) -> ModelCallResult:
        data = self._decode_object(response.body, secret=secret)
        provider_request_id = provider_request_id or self._string_or_none(
            data.get("request_id") or data.get("id")
        )
        returned_model = self._string_or_none(data.get("model"))
        if not returned_model:
            raise self._invalid_response(
                "Provider response is missing model", provider_request_id
            )
        if self.config.require_response_model_match and returned_model != self.model:
            raise ProviderError(
                ProviderErrorCode.MODEL_MISMATCH,
                "Provider returned a model different from the requested model",
                retryable=False,
                provider_request_id=provider_request_id,
            )
        try:
            usage_data = data["usage"]
            input_tokens = self._required_token(usage_data, "prompt_tokens")
            output_tokens = self._required_token(usage_data, "completion_tokens")
            total_tokens = self._required_token(usage_data, "total_tokens")
            usage = ModelUsage(input_tokens, output_tokens, total_tokens)
        except (KeyError, TypeError, ValueError) as exc:
            raise self._invalid_response(
                "Provider token usage is missing or inconsistent", provider_request_id
            ) from exc
        cost = (
            input_tokens * self.config.input_per_million_tokens
            + output_tokens * self.config.output_per_million_tokens
        ) / 1_000_000
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = self._string_or_none(choice.get("finish_reason"))
        except (KeyError, IndexError, TypeError) as exc:
            raise self._invalid_response(
                "Provider response is missing required fields",
                provider_request_id,
                usage=usage,
                estimated_cost_cny=cost,
            ) from exc
        if not isinstance(content, str):
            raise self._invalid_response(
                "Provider message content must be text",
                provider_request_id,
                usage=usage,
                estimated_cost_cny=cost,
            )
        try:
            parsed_output = json.loads(content)
        except json.JSONDecodeError as exc:
            raise self._invalid_response(
                "Provider message content is not valid JSON",
                provider_request_id,
                usage=usage,
                estimated_cost_cny=cost,
            ) from exc
        if not isinstance(parsed_output, dict):
            raise self._invalid_response(
                "Provider message content must be a JSON object",
                provider_request_id,
                usage=usage,
                estimated_cost_cny=cost,
            )
        return ModelCallResult(
            request_id=request.request_id,
            task_type=request.task_type,
            provider_id=self.provider_id,
            requested_model=self.model,
            returned_model=returned_model,
            provider_request_id=provider_request_id,
            parsed_output=parsed_output,
            usage=usage,
            estimated_cost_cny=cost,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _required_token(data: Any, key: str) -> int:
        if not isinstance(data, dict):
            raise TypeError("usage must be an object")
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError(f"{key} must be a non-negative integer")
        return value

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _invalid_response(
        message: str,
        provider_request_id: str | None,
        *,
        usage: ModelUsage | None = None,
        estimated_cost_cny: float = 0.0,
    ) -> ProviderError:
        usage = usage or ModelUsage(0, 0, 0)
        return ProviderError(
            ProviderErrorCode.INVALID_RESPONSE,
            message,
            retryable=False,
            provider_request_id=provider_request_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_cny=estimated_cost_cny,
        )

    @classmethod
    def _http_error(
        cls,
        response: HttpResponse,
        *,
        provider_request_id: str | None,
        secret: str,
    ) -> ProviderError:
        status = response.status
        if status == 401:
            code, retryable = ProviderErrorCode.AUTHENTICATION, False
        elif status == 403:
            code, retryable = ProviderErrorCode.AUTHORIZATION, False
        elif status == 408:
            code, retryable = ProviderErrorCode.TIMEOUT, True
        elif status == 429:
            code, retryable = ProviderErrorCode.RATE_LIMIT, True
        elif 500 <= status <= 599:
            code = ProviderErrorCode.TRANSIENT
            retryable = status in {500, 502, 503, 504}
        else:
            code, retryable = ProviderErrorCode.INVALID_REQUEST, False
        message = f"Provider request failed with HTTP {status}"
        try:
            decoded = json.loads(response.body.decode("utf-8"))
            if isinstance(decoded, dict):
                provider_code = decoded.get("code")
                provider_message = decoded.get("message")
                if isinstance(provider_code, str) and provider_code:
                    message += f" ({provider_code})"
                if isinstance(provider_message, str) and provider_message:
                    message += f": {provider_message}"
                provider_request_id = provider_request_id or cls._string_or_none(
                    decoded.get("request_id")
                )
        except (UnicodeError, json.JSONDecodeError):
            pass
        normalized = cls._normalized_headers(response.headers)
        retry_after = cls._retry_after(normalized.get("retry-after"))
        return ProviderError(
            code,
            redact_text(message, secrets=(secret,)),
            retryable=retryable,
            http_status=status,
            provider_request_id=provider_request_id,
            retry_after_seconds=retry_after,
            secrets=(secret,),
        )

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None
        return seconds if seconds >= 0 else None


__all__ = [
    "APPROVED_MODEL",
    "BailianConfig",
    "BailianProvider",
    "HttpResponse",
    "HttpTransport",
    "UrllibHttpTransport",
]
