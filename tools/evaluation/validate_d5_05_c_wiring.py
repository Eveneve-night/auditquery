#!/usr/bin/env python3
"""D5-05-C C 侧真实前端接线验收脚本（离线、自包含、可复跑）。

只做「前端 adapter 静态接线 + mock fetch 行为」的离线校验，不启动真实后端、
不读取真实 Key、不调用模型。真实前后端逐路由联调依赖 B 的 D5-05-B 服务接口，
在 B 接口稳定后另跑，本脚本不假装已联调。

输出脱敏机器报告到 tests/reports/d5-05-c-wiring-v1.json。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_ROOT = PROJECT_ROOT / "src" / "frontend" / "d5-console"
LIVE_TRANSPORT = CONSOLE_ROOT / "live-transport.js"
API_CLIENT = CONSOLE_ROOT / "api-client.js"
APP_JS = CONSOLE_ROOT / "app.js"
INDEX_HTML = CONSOLE_ROOT / "index.html"
LIVE_PROBE = PROJECT_ROOT / "tests" / "fixtures" / "d5-05" / "probe_live_transport.mjs"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-05-c-wiring-v1.json"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(source: str) -> str:
    stripped = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", stripped)


def _run_probe() -> dict:
    result = subprocess.run(
        ["node", str(LIVE_PROBE)], cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        return {"probeError": result.stderr}
    return json.loads(result.stdout.strip().splitlines()[-1])


def _checks() -> dict:
    live = _strip_comments(_read(LIVE_TRANSPORT))
    api = _strip_comments(_read(API_CLIENT))
    app = _strip_comments(_read(APP_JS))
    html = _read(INDEX_HTML)
    probe = _run_probe()

    checks: dict[str, bool] = {
        "live_has_csrf_header": '"x-csrf-token"' in live,
        "live_same_origin_credentials": 'credentials: "same-origin"' in live,
        "live_multipart_metadata_field": 'append("metadata"' in live,
        "live_multipart_file_field": 'append("file"' in live,
        "live_no_persistence": all(
            token not in live for token in ("localStorage", "sessionStorage", "document.cookie", "indexedDB")
        ),
        "live_no_key_logging": "console.log" not in live and "console.error" not in live and "api_key" not in live,
        "api_upload_passes_file": "file" in api and "raw" in api,
        "api_download_raw": "raw: true" in api,
        "app_wires_live_mode": "createLiveTransport" in app and "D5_LIVE_BASE_URL" in app,
        "app_data_source_label": "DATA_SOURCE_LABEL" in app,
        "index_has_mode_badge": 'id="mode-badge"' in html,
    }

    if "probeError" in probe:
        checks["probe_live_transport"] = False
        checks["live_multipart_metadata_type"] = False
        checks["live_multipart_file_type"] = False
    else:
        checks["probe_live_transport"] = all(
            probe.get(key, {}).get("ok") for key in (
                "jsonRequest", "multipart", "download", "errorNormalize", "emptyBody", "noFetchThrows"
            )
        )
        # 与 B 的 parse_document_multipart 对齐：metadata=application/json、file=application/pdf
        mp = probe.get("multipart", {})
        checks["live_multipart_metadata_type"] = mp.get("metadataType") == "application/json"
        checks["live_multipart_file_type"] = mp.get("fileType") == "application/pdf"
    return checks


def validate_all() -> dict:
    checks = _checks()
    status = "pass" if all(checks.values()) else "fail"
    return {
        "status": status,
        "task_id": "D5-05-C",
        "owner": "C",
        "evidence_kind": "offline_static_wiring_check_only",
        "business_claims_allowed": False,
        "checks": checks,
        "artifacts": {
            "live_transport": "src/frontend/d5-console/live-transport.js",
            "api_client": "src/frontend/d5-console/api-client.js",
            "app": "src/frontend/d5-console/app.js",
            "probe": "tests/fixtures/d5-05/probe_live_transport.mjs",
        },
        "boundaries": {
            "real_backend_integration": "未执行：依赖 B 的 D5-05-B（Issue #76）服务接口稳定后做最终端到端联调。",
            "install_restart_acceptance": "未执行：Docker/Windows 主启动文件为 B 所有权，本任务不碰；只提供验收探针与测试计划。",
            "multipart_format": "metadata(application/json Blob)+file(application/pdf)，已对齐 B 的 parse_document_multipart 字段与 Content-Type 约定；字段名/类型见 live-transport.js。",
            "real_api_key": "未读取；连通测试仅 mock/失败关闭。",
            "model_api": "0 调用，费用 0 元。",
        },
    }


def main() -> int:
    report = validate_all()
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
