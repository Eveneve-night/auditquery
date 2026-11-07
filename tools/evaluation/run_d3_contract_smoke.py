"""Offline entry point for the D3 20-case contract-smoke preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.agent.d3_contract_smoke import (  # noqa: E402
    D3ContractSmokeRunner,
    DEFAULT_MANIFEST_PATH,
    write_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "离线校验 D3 的 20 条契约冒烟输入；本命令不加载密钥，也不调用模型 API。"
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = D3ContractSmokeRunner(args.manifest).offline_preflight()
    if args.output is not None:
        write_report(report, args.output)
    print(
        json.dumps(
            {
                "report_type": report["report_type"],
                "manifest_id": report["manifest_id"],
                "model": report["model"],
                "summary": report["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
