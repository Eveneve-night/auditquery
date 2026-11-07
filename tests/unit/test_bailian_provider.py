from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.llm.bailian import (  # noqa: E402
    BailianConfig,
    BailianProvider,
    HttpResponse,
)
from backend.llm.contracts import ModelCallRequest, ModelMessage  # noqa: E402
from backend.llm.credentials import Credential, CredentialError  # noqa: E402
from backend.llm.errors import ProviderError, ProviderErrorCode  # noqa: E402
from backend.llm.usage import UsageLedger  # noqa: E402


CONFIG_PATH = PROJECT_ROOT / "configs" / "providers" / "bailian-qwen3.7-flash.json"
FAKE_KEY = "sk-testonly0000000000000000"


class StaticCredentialProvider:
    def load(self) -> Credential:
        return Credential(FAKE_KEY, "test-fixture")


class MissingCredentialProvider:
    def load(self) -> Credential:
        raise CredentialError("fixture failure")


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def send(self, **kwargs: object) -> HttpResponse:
        self.calls.append(kwargs)
        return self.response


class RaisingTransport:
    def __init__(self, error: ProviderError) -> None:
        self.error = error
        self.call_count = 0

    def send(self, **kwargs: object) -> HttpResponse:
        self.call_count += 1
        raise self.error


def success_response(
    *, content: str = '{"status":"ok"}', model: str = "qwen3.7-flash"
) -> HttpResponse:
    return HttpResponse(
        status=200,
        headers={"X-Request-Id": "provider-request-1"},
        body=json.dumps(
            {
                "id": "completion-1",
                "model": model,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            }
        ).encode("utf-8"),
    )


def model_request(*, max_tokens: int = 128) -> ModelCallRequest:
    return ModelCallRequest(
        request_id="local-request-1",
        task_type="intent",
        messages=(ModelMessage("user", "Return one JSON object."),),
        prompt_version="intent.v1",
        output_schema_id="intent-result.v1",
        max_completion_tokens=max_tokens,
    )


class BailianProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = BailianConfig.from_file(CONFIG_PATH)

    def make_provider(
        self,
        transport: object,
        *,
        ledger: UsageLedger | None = None,
        credential_provider: object | None = None,
    ) -> BailianProvider:
        return BailianProvider(
            self.config,
            credential_provider or StaticCredentialProvider(),  # type: ignore[arg-type]
            transport=transport,  # type: ignore[arg-type]
            usage_ledger=ledger,
        )

    def test_success_uses_only_approved_model_and_records_usage(self) -> None:
        transport = FakeTransport(success_response())
        ledger = UsageLedger()
        provider = self.make_provider(transport, ledger=ledger)

        result = provider.generate_structured(model_request())

        self.assertEqual({"status": "ok"}, result.parsed_output)
        self.assertEqual("qwen3.7-flash", result.requested_model)
        self.assertEqual("qwen3.7-flash", result.returned_model)
        self.assertEqual("provider-request-1", result.provider_request_id)
        self.assertAlmostEqual(0.000036, result.estimated_cost_cny)
        self.assertEqual(1, len(transport.calls))
        call = transport.calls[0]
        payload = json.loads(call["body"].decode("utf-8"))  # type: ignore[union-attr]
        self.assertEqual("qwen3.7-flash", payload["model"])
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["enable_thinking"])
        self.assertEqual({"type": "json_object"}, payload["response_format"])
        self.assertEqual(f"Bearer {FAKE_KEY}", call["headers"]["Authorization"])  # type: ignore[index]
        summary = ledger.summary()
        self.assertEqual(1, summary["successes"])
        self.assertEqual(120, summary["total_tokens"])
        self.assertEqual(0, summary["failures"])

    def test_http_statuses_have_stable_error_classification(self) -> None:
        cases = [
            (400, ProviderErrorCode.INVALID_REQUEST, False),
            (401, ProviderErrorCode.AUTHENTICATION, False),
            (403, ProviderErrorCode.AUTHORIZATION, False),
            (408, ProviderErrorCode.TIMEOUT, True),
            (429, ProviderErrorCode.RATE_LIMIT, True),
            (500, ProviderErrorCode.TRANSIENT, True),
            (501, ProviderErrorCode.TRANSIENT, False),
        ]
        for status, expected_code, expected_retryable in cases:
            with self.subTest(status=status):
                response = HttpResponse(
                    status=status,
                    headers={"Retry-After": "1.5"},
                    body=b'{"code":"FixtureError","message":"safe fixture"}',
                )
                ledger = UsageLedger()
                provider = self.make_provider(FakeTransport(response), ledger=ledger)

                with self.assertRaises(ProviderError) as context:
                    provider.generate_structured(model_request())

                error = context.exception
                self.assertEqual(expected_code, error.code)
                self.assertEqual(expected_retryable, error.retryable)
                self.assertEqual(status, error.http_status)
                self.assertEqual(1, ledger.summary()["failures"])
                if status == 429:
                    self.assertEqual(1.5, error.retry_after_seconds)

    def test_timeout_is_recorded_once_without_adapter_retry(self) -> None:
        transport = RaisingTransport(
            ProviderError(
                ProviderErrorCode.TIMEOUT, "fixture timeout", retryable=True
            )
        )
        ledger = UsageLedger()
        provider = self.make_provider(transport, ledger=ledger)

        with self.assertRaises(ProviderError) as context:
            provider.generate_structured(model_request())

        self.assertEqual(ProviderErrorCode.TIMEOUT, context.exception.code)
        self.assertEqual(1, transport.call_count)
        self.assertEqual(1, ledger.summary()["calls"])

    def test_provider_error_redacts_key_and_ledger_keeps_no_content(self) -> None:
        response = HttpResponse(
            status=429,
            headers={},
            body=json.dumps(
                {"code": "TooManyRequests", "message": f"do not expose {FAKE_KEY}"}
            ).encode("utf-8"),
        )
        ledger = UsageLedger()
        provider = self.make_provider(FakeTransport(response), ledger=ledger)

        with self.assertRaises(ProviderError) as context:
            provider.generate_structured(model_request())

        self.assertNotIn(FAKE_KEY, str(context.exception))
        self.assertIn("[REDACTED]", str(context.exception))
        record_text = repr(ledger.records()[0])
        self.assertNotIn(FAKE_KEY, record_text)
        self.assertNotIn("Return one JSON object", record_text)

    def test_invalid_content_json_and_model_mismatch_are_rejected(self) -> None:
        cases = [
            (
                success_response(content="not-json"),
                ProviderErrorCode.INVALID_RESPONSE,
            ),
            (
                success_response(model="qwen-plus"),
                ProviderErrorCode.MODEL_MISMATCH,
            ),
        ]
        for response, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                provider = self.make_provider(FakeTransport(response))
                with self.assertRaises(ProviderError) as context:
                    provider.generate_structured(model_request())
                self.assertEqual(expected_code, context.exception.code)

    def test_invalid_content_still_records_incurred_tokens_and_cost(self) -> None:
        ledger = UsageLedger()
        provider = self.make_provider(
            FakeTransport(success_response(content="not-json")), ledger=ledger
        )

        with self.assertRaises(ProviderError):
            provider.generate_structured(model_request())

        summary = ledger.summary(request_id="local-request-1")
        self.assertEqual(120, summary["total_tokens"])
        self.assertAlmostEqual(0.000036, summary["estimated_cost_cny"])
        self.assertEqual(1, summary["attempts"])

    def test_invalid_outer_json_and_missing_usage_are_rejected(self) -> None:
        missing_usage = json.loads(success_response().body.decode("utf-8"))
        del missing_usage["usage"]
        for body in (b"not-json", json.dumps(missing_usage).encode("utf-8")):
            with self.subTest(body=body[:20]):
                provider = self.make_provider(
                    FakeTransport(HttpResponse(status=200, headers={}, body=body))
                )
                with self.assertRaises(ProviderError) as context:
                    provider.generate_structured(model_request())
                self.assertEqual(
                    ProviderErrorCode.INVALID_RESPONSE, context.exception.code
                )

    def test_request_cap_is_enforced_before_credentials_or_network(self) -> None:
        transport = FakeTransport(success_response())
        ledger = UsageLedger()
        provider = self.make_provider(
            transport,
            ledger=ledger,
            credential_provider=MissingCredentialProvider(),
        )

        with self.assertRaises(ProviderError) as context:
            provider.generate_structured(
                model_request(max_tokens=self.config.max_completion_tokens_cap + 1)
            )

        self.assertEqual(ProviderErrorCode.INVALID_REQUEST, context.exception.code)
        self.assertEqual([], transport.calls)
        self.assertEqual(1, ledger.summary()["failures"])

    def test_missing_credential_is_a_non_retryable_configuration_error(self) -> None:
        transport = FakeTransport(success_response())
        provider = self.make_provider(
            transport, credential_provider=MissingCredentialProvider()
        )

        with self.assertRaises(ProviderError) as context:
            provider.generate_structured(model_request())

        self.assertEqual(ProviderErrorCode.CONFIGURATION, context.exception.code)
        self.assertFalse(context.exception.retryable)
        self.assertEqual([], transport.calls)

    def test_config_and_constructor_reject_any_other_model(self) -> None:
        source = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        source["model"] = "qwen-plus"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaises(ProviderError) as context:
                BailianConfig.from_file(path)
            self.assertEqual(ProviderErrorCode.CONFIGURATION, context.exception.code)

        with self.assertRaises(ProviderError):
            BailianProvider(
                replace(self.config, model="qwen-plus"), StaticCredentialProvider()
            )


if __name__ == "__main__":
    unittest.main()
