"""Run one redacted connectivity check against Alibaba Cloud Model Studio.

The API key is loaded from an explicit external file, kept only in process
memory, and never included in output or reports. This tool performs exactly
one model request per invocation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.llm.credentials import (  # noqa: E402
    CredentialError,
    ExternalFileCredentialProvider,
)


DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "providers" / "bailian-qwen3.7-flash.json"
DEFAULT_KEY_FILE = PROJECT_ROOT.parent / "api key.txt"
DEFAULT_CSV_FILE = PROJECT_ROOT.parent / "默认业务空间-apiKey-7409099.csv"
DEFAULT_REPORT = PROJECT_ROOT / "tests" / "reports" / "bailian-connectivity-qwen3.7-flash.json"


class ConnectivityError(RuntimeError):
    """A safe, user-facing connectivity error."""


def load_key(key_file: Path, csv_file: Path) -> tuple[str, str]:
    try:
        credential = ExternalFileCredentialProvider(key_file, csv_file).load()
    except CredentialError as exc:
        raise ConnectivityError(
            "No valid credential was found at the approved paths"
        ) from exc
    return credential.value, credential.source_name


def _redact(message: str, key: str) -> str:
    return message.replace(key, "[REDACTED]")[:500]


def run_check(config: dict[str, Any], key: str, key_source: str) -> dict[str, Any]:
    model = config["model"]
    request_config = config["request"]
    endpoint = config["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是API连通测试器。只输出一个JSON对象，不要附加说明。",
            },
            {"role": "user", "content": '返回：{"status":"ok"}'},
        ],
        "temperature": request_config["temperature"],
        "max_completion_tokens": request_config["max_completion_tokens"],
        "stream": request_config["stream"],
        "enable_thinking": request_config["enable_thinking"],
        "response_format": {"type": "json_object"},
    }
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "challenge08-connectivity-check/1.0",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=request_config["timeout_seconds"]) as response:
            http_status = response.status
            response_headers = response.headers
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ConnectivityError(
            f"HTTP {exc.code}: {_redact(body, key)}"
        ) from exc
    except URLError as exc:
        raise ConnectivityError(f"Network error: {_redact(str(exc.reason), key)}") from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    try:
        data = json.loads(body)
        choice = data["choices"][0]
        content = choice["message"]["content"]
        parsed_content = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ConnectivityError("The API returned an unexpected or non-JSON response") from exc
    if parsed_content.get("status") != "ok":
        raise ConnectivityError("The model response did not contain status=ok")

    usage = data.get("usage", {})
    input_tokens = int(usage.get("prompt_tokens", 0))
    output_tokens = int(usage.get("completion_tokens", 0))
    pricing = config["pricing_snapshot"]
    estimated_cost = (
        input_tokens * pricing["input_per_million_tokens"]
        + output_tokens * pricing["output_per_million_tokens"]
    ) / 1_000_000
    request_id = (
        response_headers.get("x-request-id")
        or response_headers.get("request-id")
        or data.get("request_id")
        or data.get("id")
    )
    return {
        "status": "pass",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "provider_id": config["provider_id"],
        "region": config["region"],
        "workspace_type": config["workspace_type"],
        "protocol": config["protocol"],
        "model_requested": model,
        "model_returned": data.get("model"),
        "credential_source": key_source,
        "http_status": http_status,
        "request_id": request_id,
        "latency_ms": elapsed_ms,
        "finish_reason": choice.get("finish_reason"),
        "response_valid_json": True,
        "response_content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest().upper(),
        "response_content_chars": len(content),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": int(usage.get("total_tokens", input_tokens + output_tokens)),
        },
        "estimated_cost_cny": round(estimated_cost, 8),
        "secret_logged": False,
        "network_requests": 1,
        "model_api_calls": 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--csv-key-file", type=Path, default=DEFAULT_CSV_FILE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    key = ""
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        key, key_source = load_key(args.key_file, args.csv_key_file)
        report = run_check(config, key, key_source)
    except (OSError, KeyError, TypeError, ValueError, ConnectivityError) as exc:
        report = {
            "status": "fail",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error": _redact(str(exc), key) if key else str(exc)[:500],
            "secret_logged": False,
            "network_requests": 1 if key else 0,
            "model_api_calls": 1 if key else 0,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
