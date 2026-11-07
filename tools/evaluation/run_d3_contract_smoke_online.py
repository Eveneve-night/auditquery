"""Explicitly authorized online entry point for the D3 contract smoke.

This command is deliberately separate from the offline preflight. It refuses to
run unless the caller supplies both an explicit case limit and the exact
confirmation phrase. Credentials are loaded only by the approved external-file
provider and are never printed or written to the report.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.d3_contract_smoke import (  # noqa: E402
    ContractSmokeError,
    D3ContractSmokeRunner,
    DEFAULT_MANIFEST_PATH,
    write_report,
)
from backend.llm import (  # noqa: E402
    BailianConfig,
    BailianProvider,
    CredentialError,
    ExternalFileCredentialProvider,
    LLMGateway,
    ProviderError,
    UsageLedger,
)


CONFIRMATION_PHRASE = "D3-05-qwen3.7-flash"
DEFAULT_PROVIDER_CONFIG = (
    PROJECT_ROOT / "configs" / "providers" / "bailian-qwen3.7-flash.json"
)
DEFAULT_REPORT = (
    PROJECT_ROOT
    / "tests"
    / "reports"
    / "d3-contract-smoke-qwen3.7-flash-v1.json"
)


class OnlineSmokeSafetyError(RuntimeError):
    """Raised before network access when online execution is not explicitly safe."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "执行已获人工授权的 D3-05 真实 API 契约冒烟；"
            "仅允许 qwen3.7-flash，并生成脱敏报告。"
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--provider-config", type=Path, default=DEFAULT_PROVIDER_CONFIG
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--case-limit",
        type=int,
        choices=range(1, 21),
        required=True,
        metavar="1..20",
        help="本次明确允许执行的最大案例数。",
    )
    parser.add_argument(
        "--confirm-real-api",
        required=True,
        metavar="PHRASE",
        help=f"必须精确填写 {CONFIRMATION_PHRASE!r}。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="显式允许覆盖已有脱敏报告；默认拒绝覆盖。",
    )
    return parser


def _load_provider_config(path: Path) -> tuple[BailianConfig, Mapping[str, Any]]:
    config = BailianConfig.from_file(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    credential_config = raw.get("credential")
    if not isinstance(credential_config, Mapping):
        raise OnlineSmokeSafetyError("provider credential configuration is missing")
    return config, credential_config


def execute_online_smoke(args: argparse.Namespace) -> dict[str, Any]:
    if args.confirm_real_api != CONFIRMATION_PHRASE:
        raise OnlineSmokeSafetyError("real API confirmation phrase did not match")
    output_path = Path(args.output).resolve()
    if output_path.exists() and not args.overwrite:
        raise OnlineSmokeSafetyError(
            "report already exists; use --overwrite only after explicit rerun approval"
        )

    runner = D3ContractSmokeRunner(args.manifest)
    preflight = runner.offline_preflight()
    if preflight["summary"]["api_calls"] != 0:
        raise OnlineSmokeSafetyError("offline preflight unexpectedly used the API")
    if args.case_limit > preflight["summary"]["prepared_case_count"]:
        raise OnlineSmokeSafetyError("case limit exceeds prepared manifest size")

    provider_config, credential_config = _load_provider_config(args.provider_config)
    credential_provider = ExternalFileCredentialProvider.from_config(
        credential_config
    )
    usage_ledger = UsageLedger()
    provider = BailianProvider(
        provider_config,
        credential_provider,
        usage_ledger=usage_ledger,
    )
    gateway = LLMGateway(
        provider,
        runner.policy,
        usage_ledger=usage_ledger,
    )
    report = runner.run(gateway, case_limit=args.case_limit)
    report["execution_safety"] = {
        "human_authorization_required": True,
        "explicit_confirmation_received": True,
        "credential_persisted": False,
        "raw_prompt_persisted": False,
        "raw_response_persisted": False,
        "candidate_sql_executed": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, output_path)
    return report


def public_summary(report: Mapping[str, Any], output_path: Path) -> dict[str, Any]:
    summary = report["summary"]
    return {
        "report_type": report["report_type"],
        "purpose": report["purpose"],
        "model": report["model"],
        "source_split": report["source_split"],
        "output": str(output_path.resolve()),
        "summary": {
            "planned_case_count": summary["planned_case_count"],
            "attempted_case_count": summary["attempted_case_count"],
            "not_run_count": summary["not_run_count"],
            "contract_valid_count": summary["contract_valid_count"],
            "failed_count": summary["failed_count"],
            "model_attempt_count": summary["model_attempt_count"],
            "input_tokens": summary["input_tokens"],
            "output_tokens": summary["output_tokens"],
            "total_tokens": summary["total_tokens"],
            "estimated_cost_cny": summary["estimated_cost_cny"],
            "elapsed_ms": summary["elapsed_ms"],
            "batch_stop_reason": summary["batch_stop_reason"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = execute_online_smoke(args)
    except ProviderError as exc:
        print(
            json.dumps(
                {"status": "failed", "error_code": exc.code.value},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except (CredentialError, ContractSmokeError, OnlineSmokeSafetyError, OSError,
            UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "failed", "error_code": type(exc).__name__},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(public_summary(report, args.output), ensure_ascii=False, indent=2)
    )
    summary = report["summary"]
    complete = (
        summary["attempted_case_count"] == summary["planned_case_count"]
        and summary["contract_valid_count"] == summary["planned_case_count"]
        and summary["not_run_count"] == 0
    )
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
