#!/usr/bin/env python3
"""D5-05 合并后 A 侧部署 Gate。

本工具只使用公开仓库资产、明显的测试密钥和 localhost。它不会读取真实
API Key，不会调用模型，也不会把问题、答案、SQL、文档正文或凭据写入报告。
环境相关结论按实际能力分层；缺少 Docker 或 Windows 启动失败时必须阻塞，
不能用组件单测冒充独立部署通过。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from backend.product.server import ProductRuntime, ProductStartupError  # noqa: E402
from infra.product_snapshot import backup, restore  # noqa: E402
from tools.evaluation.import_d5_05_actions_receipt import (  # noqa: E402
    ReceiptError,
    validate_receipt,
)


REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-05-postmerge-gate-v2.json"
ACTIONS_RECEIPT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-05-trusted-actions-evidence-v1.json"
BOOTSTRAP_PATH = PROJECT_ROOT / "configs" / "contracts" / "d5" / "admin-bootstrap.v2.json"
PDF_FIXTURE = PROJECT_ROOT / "data" / "raw" / "pdf" / "sales_playbook.pdf"
WINDOWS_SCRIPT = PROJECT_ROOT / "infra" / "start.ps1"
FAKE_MASTER_KEY = b"d5-gate-test-master-key-32-bytes!"[:32]
FAKE_API_KEY = "testonly-d505-postmerge-gate-credential"
BASELINE_MAIN_SHA = "4bc170cd895dff040058d71888f946379267f67a"
TRUSTED_ACTIONS_HEAD_SHA = "4bc170cd895dff040058d71888f946379267f67a"
MERGED_PREREQUISITES = {
    "pr_79_backend_package": "5504c15e90b6e00a1952c9b4a8ae6747cf7591a8",
    "pr_78_frontend_wiring": "4f57aba02e97a6cf70987fad1c73798d9ee27238",
    "pr_84_windows_start": "8f378c13bc561761a3edbefb618fca39955df2c8",
    "pr_83_visitor_rag_five_turn": "630d997b1c153024ad300ce8bb7f8e26b3c77d61",
    "pr_86_trusted_deployment_evidence": "2bb43f962b10ca9cb88f04be7e96ebadec4ebbd6",
    "pr_87_live_frontend": "3530ad975c48083d68ceb6ca989b061d2b4e4c31",
}
SOURCE_MANIFEST_SCHEMA_VERSION = "d5-gate-evaluated-source.v1"
SOURCE_MANIFEST_PREFIXES = (
    "configs/",
    "data/",
    "domain-packs/",
    "infra/",
    "src/",
    "tests/",
    "tools/",
)
SOURCE_MANIFEST_TOP_LEVEL_FILES = (
    ".dockerignore",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    ".github/workflows/d5-05-sandbox.yml",
    "Dockerfile",
    "compose.yaml",
    "requirements-dev.txt",
    "requirements-product.txt",
    "README.md",
)
SOURCE_MANIFEST_EXCLUDED_PATHS = (
    "tests/reports/d5-05-postmerge-gate-v2.json",
    "tests/reports/d5-06-07-final-gate-v1.json",
)
REQUIRED_SOURCE_PATHS = (
    ".github/workflows/d5-05-sandbox.yml",
    "Dockerfile",
    "compose.yaml",
    "infra/start.ps1",
    "requirements-product.txt",
    "README.md",
    "configs/contracts/d5/product-layer.v1.json",
    "data/raw/chinook-v1.4.5/Chinook_Sqlite.sqlite",
    "data/raw/pdf/sales_playbook.pdf",
    "domain-packs/chinook/v1/manifest.json",
    "src/backend/product/server.py",
    "src/frontend/d5-console/index.html",
    "tests/fixtures/d5-05/probe_live_transport.mjs",
    "tests/unit/test_d5_product_server.py",
    "tests/reports/d5-05-trusted-actions-evidence-v1.json",
    "tests/reports/d5-06-b-backend-security-v1.json",
    "tests/reports/d5-06-c-machine-report-v1.json",
    "tests/unit/test_d5_06_07_final_gate.py",
    "tools/deployment/d5_05_actions_evidence.py",
    "tools/evaluation/import_d5_05_actions_receipt.py",
    "tools/evaluation/validate_d5_05_postmerge_gate.py",
    "tools/evaluation/validate_d5_06_07_final_gate.py",
    "tools/evaluation/validate_d5_06_b_backend_security.py",
    "tools/evaluation/validate_d5_06_c_machine_report.py",
)


class GateProbeError(RuntimeError):
    """Stable, content-safe probe failure."""


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if completed.returncode != 0:
        raise GateProbeError("git_probe_failed")
    return completed.stdout.strip()


def _git_bytes(*args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise GateProbeError("git_probe_failed")
    return completed.stdout


def _is_ancestor(commit: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0


def _source_path_is_in_scope(relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    if normalized in SOURCE_MANIFEST_EXCLUDED_PATHS:
        return False
    return normalized in SOURCE_MANIFEST_TOP_LEVEL_FILES or normalized.startswith(SOURCE_MANIFEST_PREFIXES)


def _source_manifest_for_commit(commit: str) -> dict[str, Any]:
    resolved_commit = _git("rev-parse", "--verify", f"{commit}^{{commit}}")
    tree_oid = _git("rev-parse", "--verify", f"{resolved_commit}^{{tree}}")
    entries: list[dict[str, str]] = []
    for record in _git_bytes("ls-tree", "-r", "-z", "--full-tree", resolved_commit).split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ")
            relative = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GateProbeError("source_manifest_parse_failed") from exc
        if not _source_path_is_in_scope(relative):
            continue
        if object_type != "blob":
            raise GateProbeError("source_manifest_non_blob_entry")
        entries.append({"path": relative, "mode": mode, "blob_oid": object_id})
    entries.sort(key=lambda item: item["path"])
    present = {item["path"] for item in entries}
    if not set(REQUIRED_SOURCE_PATHS).issubset(present):
        raise GateProbeError("source_manifest_required_path_missing")
    hash_payload = {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "included_prefixes": list(SOURCE_MANIFEST_PREFIXES),
        "included_top_level_files": list(SOURCE_MANIFEST_TOP_LEVEL_FILES),
        "excluded_paths": list(SOURCE_MANIFEST_EXCLUDED_PATHS),
        "files": entries,
    }
    return {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "evaluated_commit_sha": resolved_commit,
        "evaluated_tree_oid": tree_oid,
        "binding_rule": "evaluated_commit_ancestor_and_complete_source_manifest_equal",
        "report_self_reference_excluded": True,
        "included_prefixes": list(SOURCE_MANIFEST_PREFIXES),
        "included_top_level_files": list(SOURCE_MANIFEST_TOP_LEVEL_FILES),
        "excluded_paths": list(SOURCE_MANIFEST_EXCLUDED_PATHS),
        "file_count": len(entries),
        "files": entries,
        "source_manifest_stable_sha256": _canonical_hash(hash_payload),
    }


def _source_pathspecs() -> list[str]:
    included = [f":(top){path.rstrip('/')}" for path in SOURCE_MANIFEST_PREFIXES]
    included.extend(f":(top){path}" for path in SOURCE_MANIFEST_TOP_LEVEL_FILES)
    included.extend(f":(top,exclude){path}" for path in SOURCE_MANIFEST_EXCLUDED_PATHS)
    return included


def _assert_source_worktree_clean() -> None:
    pathspecs = _source_pathspecs()
    for command in (
        ["git", "diff", "--quiet", "HEAD", "--", *pathspecs],
        ["git", "diff", "--cached", "--quiet", "HEAD", "--", *pathspecs],
    ):
        completed = subprocess.run(
            command, cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False,
        )
        if completed.returncode == 1:
            raise GateProbeError("evaluated_source_worktree_dirty")
        if completed.returncode != 0:
            raise GateProbeError("git_probe_failed")
    untracked = _git_bytes("ls-files", "--others", "--exclude-standard", "-z", "--", *pathspecs)
    if untracked:
        raise GateProbeError("evaluated_source_worktree_dirty")


def _verify_evaluated_source(evaluated_commit: str | None = None) -> dict[str, Any]:
    selected_commit = evaluated_commit or _git("rev-parse", "HEAD")
    if not _is_ancestor(selected_commit):
        raise GateProbeError("evaluated_commit_not_ancestor")
    evaluated = _source_manifest_for_commit(selected_commit)
    current = _source_manifest_for_commit("HEAD")
    if (
        evaluated["source_manifest_stable_sha256"] != current["source_manifest_stable_sha256"]
        or evaluated["files"] != current["files"]
    ):
        raise GateProbeError("evaluated_source_changed")
    _assert_source_worktree_clean()
    return evaluated


def _trusted_actions_probe() -> dict[str, Any]:
    try:
        receipt = json.loads(ACTIONS_RECEIPT_PATH.read_text(encoding="utf-8"))
        validate_receipt(receipt, expected_head_sha=TRUSTED_ACTIONS_HEAD_SHA)
    except (OSError, json.JSONDecodeError, ReceiptError):
        return {"status": "fail", "error_code": "trusted_actions_receipt_invalid"}
    source = receipt["source"]
    jobs = receipt["jobs"]
    assert isinstance(source, dict) and isinstance(jobs, list)
    summaries = []
    for item in jobs:
        assert isinstance(item, dict)
        artifact = item["artifact"]
        runner = item["runner"]
        checks = item["checks"]
        assert isinstance(artifact, dict) and isinstance(runner, dict) and isinstance(checks, dict)
        summaries.append(
            {
                "job": item["job"],
                "runner_os": runner.get("os"),
                "artifact_id": artifact.get("id"),
                "artifact_digest": artifact.get("archive_digest"),
                "evidence_stable_sha256": item.get("evidence_stable_sha256"),
                "checks": sorted(checks),
            }
        )
    return {
        "status": "pass",
        "trust_source": "github_actions_api_v1",
        "repository": source["repository"],
        "run_id": source["run_id"],
        "run_attempt": source["run_attempt"],
        "run_url": source["run_url"],
        "event": source["event"],
        "branch": source["branch"],
        "head_sha": source["head_sha"],
        "receipt_stable_sha256": receipt["stable_sha256"],
        "jobs": summaries,
        "self_asserted_artifact_is_sufficient": False,
        "non_author_must_reverify_github_api": True,
    }


def _run_tests(targets: list[str] | None = None) -> dict[str, Any]:
    command = [sys.executable, "-B", "-m", "unittest"]
    if targets is None:
        command.extend(["discover", "-s", "tests/unit", "-p", "test_*.py"])
    else:
        command.extend(targets)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", env=env, check=False
    )
    combined = completed.stdout + "\n" + completed.stderr
    import re
    match = re.search(r"Ran\s+(\d+)\s+tests?", combined)
    return {
        "status": "pass" if completed.returncode == 0 and match else "fail",
        "tests_run": int(match.group(1)) if match else 0,
        "failures": 0 if completed.returncode == 0 else 1,
    }


def _seed_probe() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-B", "tools/evaluation/validate_seed_assets.py"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return {"status": "fail", "error_code": "seed_report_invalid"}
    return {
        "status": "pass" if completed.returncode == 0 and payload.get("status") == "pass" else "fail",
        "total_cases": payload.get("total_cases", 0),
        "validation_cases_checked": payload.get("split_counts", {}).get("validation", 0),
        "model_api_calls": payload.get("model_api_calls", 0),
        "network_calls": payload.get("network_calls", 0),
    }


def _free_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict[str, str], bytes]:
    url = f"http://127.0.0.1:{port}{path}"
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with (opener.open(request, timeout=5) if opener else urllib.request.urlopen(request, timeout=5)) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _json_request(
    port: int,
    method: str,
    path: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    merged = {"Content-Type": "application/json", **(headers or {})}
    status, response_headers, body = _request(
        port, method, path, body=json.dumps(payload).encode("utf-8"), headers=merged, opener=opener
    )
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return status, response_headers, parsed


def _start_product(instance: Path, port: int, *, mode: str = "offline") -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.pop("ICT_MASTER_KEY_FILE", None)
    env["ICT_SECRET_MASTER_KEY_B64"] = base64.b64encode(FAKE_MASTER_KEY).decode("ascii")
    env["PYTHONPATH"] = str(SRC_ROOT)
    process = subprocess.Popen(
        [sys.executable, "-B", "-m", "backend.product.server", "serve", "--instance", str(instance),
         "--mode", mode, "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if process.poll() is not None:
            raise GateProbeError("product_server_exited")
        try:
            if _request(port, "GET", "/health/ready")[0] == 200:
                return process
        except OSError:
            pass
        time.sleep(0.05)
    process.terminate()
    process.wait(timeout=5)
    raise GateProbeError("product_server_not_ready")


def _stop_product(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _login(port: int, password: str) -> tuple[urllib.request.OpenerDirector, str, dict[str, Any]]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, _headers, payload = _json_request(
        port, "POST", "/api/v3/auth/admin/login",
        {"api_version": "3.0", "contract_version": "2.0.0", "request_id": "req.gate.login",
         "username": "admin", "password": password}, opener=opener,
    )
    if status != 200 or not any(cookie.name == "ict_admin_session" for cookie in jar):
        raise GateProbeError("admin_login_failed")
    csrf = payload.get("csrf_token")
    if not isinstance(csrf, str) or not csrf:
        raise GateProbeError("csrf_missing")
    return opener, csrf, payload


def _multipart(metadata: dict[str, Any], pdf: bytes) -> tuple[bytes, str]:
    boundary = b"d5gateboundary"
    meta = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    body = (
        b"--" + boundary + b'\r\nContent-Disposition: form-data; name="metadata"\r\n'
        b"Content-Type: application/json\r\n\r\n" + meta + b"\r\n--" + boundary
        + b'\r\nContent-Disposition: form-data; name="file"; filename="gate.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n" + pdf + b"\r\n--" + boundary + b"--\r\n"
    )
    return body, "multipart/form-data; boundary=d5gateboundary"


def _runtime_persistence_probe() -> dict[str, Any]:
    contract = json.loads(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    password = contract["initial_credentials"]["initial_password"]
    with tempfile.TemporaryDirectory(prefix="d5-gate-") as directory:
        root = Path(directory)
        instance, snapshot, restored = root / "instance", root / "snapshot", root / "restored"
        port = _free_port()
        first = _start_product(instance, port)
        document_id = ""
        try:
            root_status, _headers, root_body = _request(port, "GET", "/")
            root_text = root_body.decode("utf-8", errors="replace")
            opener, csrf, login = _login(port, password)
            pdf = PDF_FIXTURE.read_bytes()
            upload_request_id = "req.gate.upload"
            metadata = {
                "api_version": "3.0", "contract_version": "1.0.0", "request_id": upload_request_id,
                "filename": "gate.pdf", "content_type": "application/pdf", "declared_bytes": len(pdf),
                "content_sha256": hashlib.sha256(pdf).hexdigest(),
            }
            upload_body, content_type = _multipart(metadata, pdf)
            upload_status, _headers, upload_raw = _request(
                port, "POST", "/api/v3/admin/documents", body=upload_body,
                headers={"Content-Type": content_type, "X-CSRF-Token": csrf,
                         "X-Request-ID": upload_request_id}, opener=opener,
            )
            upload = json.loads(upload_raw.decode("utf-8")) if upload_raw else {}
            document_id = str(upload.get("document_id", ""))
            publish_status, _headers, publish = _json_request(
                port, "POST", f"/api/v3/admin/documents/{document_id}/publish", {},
                headers={"X-CSRF-Token": csrf, "X-Request-ID": "req.gate.publish"}, opener=opener,
            )
            secret_status, _headers, secret = _json_request(
                port, "POST", "/api/v3/admin/secrets/bailian",
                {"api_version": "3.0", "contract_version": "1.0.0", "request_id": "req.gate.secret",
                 "provider": "bailian_beijing", "model": "qwen3.7-flash", "api_key": FAKE_API_KEY},
                headers={"X-CSRF-Token": csrf, "X-Request-ID": "req.gate.secret"}, opener=opener,
            )
            visitor_status, visitor_headers, visitor = _json_request(
                port, "POST", "/api/v3/sessions",
                {"api_version": "3.0", "contract_version": "1.0.0", "request_id": "req.gate.visitor"},
            )
            visitor_cookie = visitor_headers.get("Set-Cookie", "").split(";", 1)[0]
            query_status, _query_headers, query = _json_request(
                port, "POST", "/api/v3/query",
                {"api_version": "3.0", "contract_version": "1.0.0", "request_id": "req.gate.query",
                 "session_id": visitor.get("session_id"), "turn_id": 0, "state_revision": 0,
                 "question": "查询刚发布文档"}, headers={"Cookie": visitor_cookie},
            )
        finally:
            _stop_product(first)

        second = _start_product(instance, port)
        try:
            restarted_opener, _csrf, restarted_login = _login(port, password)
            health_status, _headers, health = _request(port, "GET", "/api/v3/admin/health", opener=restarted_opener)
            health_payload = json.loads(health.decode("utf-8")) if health else {}
        finally:
            _stop_product(second)

        reopened = ProductRuntime(instance, master_key=FAKE_MASTER_KEY)
        active_index = reopened.documents.get_active_index()
        record = reopened.documents.get_record(document_id) if document_id else None
        backup(instance, snapshot)
        restore(snapshot, restored)
        restored_runtime = ProductRuntime(restored, master_key=FAKE_MASTER_KEY)
        wrong_key_rejected = False
        try:
            ProductRuntime(instance, master_key=b"x" * 32)
        except ProductStartupError:
            wrong_key_rejected = True
        snapshot_manifest = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
        secret_plaintext_absent = all(
            FAKE_API_KEY.encode("utf-8") not in path.read_bytes()
            for path in instance.rglob("*") if path.is_file()
        )
        password_plaintext_absent = all(
            password.encode("utf-8") not in path.read_bytes()
            for path in instance.rglob("*") if path.is_file()
        )
        return {
            "localhost_http_started": True,
            "visitor_home_without_login": root_status == 200 and 'href="#/admin/settings"' in root_text,
            "admin_v2_optional_password": (
                login.get("must_change_password") is False
                and login.get("allowed_actions") == ["admin", "change_password", "logout"]
            ),
            "multipart_upload": upload_status in {200, 201} and bool(document_id),
            "document_publish": publish_status == 200 and publish.get("state") == "published",
            "fake_secret_saved_without_echo": secret_status == 200 and FAKE_API_KEY not in json.dumps(secret),
            "process_restart": True,
            "admin_persisted": reopened.auth.store.read_state()["username"] == "admin",
            "document_persisted": record is not None and record.state == "published",
            "index_version_persisted": isinstance(active_index, str) and bool(active_index),
            "secret_configured_persisted": reopened.secret_store.status().configured,
            "admin_health_after_restart": health_status == 200 and health_payload.get("status") == "ok",
            "backup_restore": (
                restored_runtime.auth.store.read_state()["username"] == "admin"
                and restored_runtime.secret_store.status().configured
                and restored_runtime.documents.get_active_index() == active_index
            ),
            "snapshot_excludes_master_key": snapshot_manifest.get("master_key_included") is False,
            "wrong_master_key_fails_closed": wrong_key_rejected,
            "fake_key_plaintext_absent": secret_plaintext_absent,
            "password_plaintext_absent": password_plaintext_absent,
            "offline_query_fails_closed": query_status == 503 and query.get("error", {}).get("code") == "service_unavailable",
        }


def _visitor_product_probe() -> dict[str, Any]:
    """Exercise the published-document RAG and five-turn flow over real localhost HTTP."""

    contract = json.loads(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    password = contract["initial_credentials"]["initial_password"]
    process: subprocess.Popen[bytes] | None = None
    temporary = tempfile.TemporaryDirectory(prefix="d5-gate-visitor-", ignore_cleanup_errors=True)
    try:
        instance = Path(temporary.name) / "instance"
        port = _free_port()
        process = _start_product(instance, port, mode="online")
        opener, csrf, _login_payload = _login(port, password)
        pdf = PDF_FIXTURE.read_bytes()
        upload_request_id = "req.gate.visitor.upload"
        metadata = {
            "api_version": "3.0", "contract_version": "1.0.0",
            "request_id": upload_request_id, "filename": "gate.pdf",
            "content_type": "application/pdf", "declared_bytes": len(pdf),
            "content_sha256": hashlib.sha256(pdf).hexdigest(),
        }
        upload_body, content_type = _multipart(metadata, pdf)
        upload_status, _headers, upload_raw = _request(
            port, "POST", "/api/v3/admin/documents", body=upload_body,
            headers={"Content-Type": content_type, "X-CSRF-Token": csrf,
                     "X-Request-ID": upload_request_id}, opener=opener,
        )
        upload = json.loads(upload_raw.decode("utf-8")) if upload_raw else {}
        document_id = upload.get("document_id")
        publish_status, _headers, publish = _json_request(
            port, "POST", f"/api/v3/admin/documents/{document_id}/publish", {},
            headers={"X-CSRF-Token": csrf, "X-Request-ID": "req.gate.visitor.publish"},
            opener=opener,
        )
        create_status, create_headers, created = _json_request(
            port, "POST", "/api/v3/sessions",
            {"api_version": "3.0", "contract_version": "1.0.0",
             "request_id": "req.gate.visitor.session"},
        )
        cookie = create_headers.get("Set-Cookie", "").split(";", 1)[0]
        session_id = created.get("session_id")
        turn = 0
        revision = 0
        responses: list[dict[str, Any]] = []

        def query(request_id: str, question: str) -> tuple[int, dict[str, Any]]:
            nonlocal turn, revision
            status, _response_headers, payload = _json_request(
                port, "POST", "/api/v3/query",
                {"api_version": "3.0", "contract_version": "1.0.0",
                 "request_id": request_id, "session_id": session_id,
                 "turn_id": turn, "state_revision": revision, "question": question},
                headers={"Cookie": cookie},
            )
            responses.append(payload)
            if status == 200 and isinstance(payload.get("state_revision"), int):
                revision = payload["state_revision"]
                turn += 1
            return status, payload

        first_status, first = query("req.gate.visitor.turn1", "销售流程")
        first_payload = first.get("payload", {})
        citations = first_payload.get("citations", [])
        citation = citations[0] if isinstance(citations, list) and citations else {}

        # Exercise missing-metric clarification, not a now-complete count query.
        second_status, second = query("req.gate.visitor.turn2", "艺术家")
        pending = second.get("payload", {}).get("pending_question", {})
        options = pending.get("options", []) if isinstance(pending, dict) else []
        option = options[0] if isinstance(options, list) and options else {}
        clarification_status, _headers, clarification = _json_request(
            port, "POST", "/api/v3/clarifications",
            {"api_version": "3.0", "contract_version": "1.0.0",
             "request_id": "req.gate.visitor.turn2", "answer_id": "ans.gate.visitor.turn3",
             "session_id": session_id, "turn_id": pending.get("turn_id"),
             "state_revision": pending.get("state_revision"),
             "question_id": pending.get("question_id"), "slot_id": pending.get("slot_id"),
             "values": [option.get("value")], "evidence_refs": [option.get("evidence_ref")]},
            headers={"Cookie": cookie},
        )
        responses.append(clarification)
        if clarification_status == 200 and isinstance(clarification.get("state_revision"), int):
            revision = clarification["state_revision"]
            turn += 1

        fourth_status, fourth = query("req.gate.visitor.turn4", "月度经营报表的截止时间")
        fifth_status, fifth = query("req.gate.visitor.turn5", "区块链共识算法")

        model_calls = sum(
            int(response.get("payload", {}).get("usage", {}).get("model_calls", 0))
            for response in responses if isinstance(response, dict)
        )
        same_session = all(response.get("session_id") == session_id for response in responses)
        citation_valid = (
            citation.get("document_id") == document_id
            and isinstance(citation.get("page"), int) and citation["page"] >= 1
            and isinstance(citation.get("evidence_id"), str)
            and citation["evidence_id"].startswith(f"{document_id}.p")
        )
        retrieval_passed = (
            upload_status in {200, 201} and publish_status == 200
            and publish.get("state") == "published" and create_status == 200
            and first_status == 200 and first.get("status") == "success"
            and first_payload.get("route") == "rag" and citation_valid
        )
        clarification_passed = (
            second_status == 200 and second.get("status") == "clarification_required"
            and clarification_status == 200
            and clarification.get("status") == "clarification_required"
        )
        fourth_payload = fourth.get("payload", {})
        no_evidence_passed = (
            fifth_status == 200 and fifth.get("status") != "success"
            and fifth.get("payload", {}).get("citations", []) == []
        )
        five_turns_passed = (
            turn == 5 and revision == 5 and same_session and retrieval_passed
            and clarification_passed and fourth_status == 200
            and fourth.get("status") == "success" and fourth_payload.get("route") == "rag"
            and no_evidence_passed and model_calls == 0
        )
        passed = retrieval_passed and five_turns_passed
        return {
            "status": "pass" if passed else "fail",
            "actual_http": True,
            "published_document_retrieved": retrieval_passed,
            "citation_reference_valid": citation_valid,
            "same_session_five_turn": five_turns_passed,
            "turns_completed": turn,
            "clarification_round_trip": clarification_passed,
            "no_evidence_fails_closed": no_evidence_passed,
            "model_api_calls": model_calls,
            "localhost_only": True,
        }
    except (GateProbeError, OSError, ValueError, KeyError, TypeError):
        return {"status": "fail", "error_code": "visitor_product_probe_failed", "model_api_calls": 0}
    finally:
        if process is not None:
            _stop_product(process)
        temporary.cleanup()


def _wait_ready(port: int, process: subprocess.Popen[bytes]) -> bool:
    for _ in range(120):
        if process.poll() is not None:
            return False
        try:
            if _request(port, "GET", "/health/ready")[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def _stop_windows_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    taskkill = shutil.which("taskkill")
    if taskkill:
        subprocess.run(
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _windows_probe(python312: Path | None) -> dict[str, Any]:
    if os.name != "nt":
        return {"status": "blocked", "reason": "not_windows"}
    powershell = shutil.which("powershell")
    if not powershell:
        return {"status": "blocked", "reason": "powershell_missing"}
    script_path = str(WINDOWS_SCRIPT).replace("'", "''")
    parse_command = (
        "$tokens=$null;$errors=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{script_path}',"
        "[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count -gt 0){exit 2}"
    )
    parsed = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", parse_command],
        cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if parsed.returncode != 0:
        return {
            "status": "fail", "invocation_attempted": True,
            "error_code": "windows_powershell_parse_error", "start_reached": False,
        }
    if python312 is None or not python312.is_file():
        return {"status": "blocked", "reason": "python_3_12_missing", "script_parse": "pass"}
    version = subprocess.run(
        [str(python312), "-c", "import sys;print(sys.version_info[0]*100+sys.version_info[1])"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if version.returncode != 0 or version.stdout.strip() != "312":
        return {"status": "blocked", "reason": "python_3_12_required", "script_parse": "pass"}
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="d5-gate-windows-") as directory:
            instance = Path(directory) / "instance"
            port = _free_port()
            command = [
                powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(WINDOWS_SCRIPT), "-InstanceDir", str(instance),
                "-Mode", "online", "-PythonExe", str(python312),
                "-UseExistingEnvironment", "-Port", str(port),
            ]
            env = os.environ.copy()
            env.pop("ICT_MASTER_KEY_FILE", None)
            env["ICT_SECRET_MASTER_KEY_B64"] = base64.b64encode(FAKE_MASTER_KEY).decode("ascii")
            starts = []
            for _attempt in range(2):
                process = subprocess.Popen(
                    command, cwd=PROJECT_ROOT, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                starts.append(_wait_ready(port, process))
                _stop_windows_process_tree(process)
                process = None
            passed = starts == [True, True]
            return {
                "status": "pass" if passed else "fail",
                "script_parse": "pass", "python_3_12": "available",
                "mode": "online", "first_start_ready": starts[0],
                "restart_ready": starts[1], "localhost_only": True,
                **({} if passed else {"error_code": "windows_online_start_restart_failed"}),
            }
    except (OSError, subprocess.SubprocessError):
        return {
            "status": "fail", "script_parse": "pass", "python_3_12": "available",
            "error_code": "windows_online_start_restart_failed",
        }
    finally:
        if process is not None:
            _stop_windows_process_tree(process)


def _docker_probe() -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        return {"status": "blocked", "reason": "docker_cli_missing", "commands_executed": 0}
    version = subprocess.run([docker, "version"], capture_output=True, check=False)
    compose = subprocess.run([docker, "compose", "version"], capture_output=True, check=False)
    if version.returncode != 0 or compose.returncode != 0:
        return {"status": "blocked", "reason": "docker_engine_or_compose_unavailable", "commands_executed": 2}
    return {"status": "blocked", "reason": "docker_build_up_restart_not_executed", "commands_executed": 2}


def build_report(
    *,
    run_tests: bool = True,
    python312: Path | None = None,
    evaluated_commit: str | None = None,
    evaluated_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_binding = evaluated_source or _verify_evaluated_source(evaluated_commit)
    full = _run_tests() if run_tests else {"status": "not_run", "tests_run": 0, "failures": 0}
    relevant = _run_tests([
        "tests.unit.test_d5_product_server", "tests.unit.test_d5_visitor",
        "tests.unit.test_d5_product_snapshot", "tests.unit.test_d5_frontend_console",
        "tests.unit.test_d5_frontend_live_wiring", "tests.unit.test_d5_frontend_install_restart_probe",
        "tests.unit.test_d5_auth_rbac", "tests.unit.test_d5_secret_store",
        "tests.unit.test_d5_document_lifecycle", "tests.unit.test_d4_frontend_console",
        "tests.unit.test_d4_frontend_backend_link", "tests.unit.test_d5_windows_start",
        "tests.unit.test_d5_rag_published_retriever", "tests.unit.test_d5_online_rag_service",
        "tests.unit.test_d5_visitor_rag_dispatcher", "tests.unit.test_d5_visitor_rag_e2e",
        "tests.unit.test_d5_visitor_rag_http_e2e",
    ]) if run_tests else {"status": "not_run", "tests_run": 0, "failures": 0}
    seed = _seed_probe() if run_tests else {"status": "not_run", "total_cases": 0}
    runtime = _runtime_persistence_probe()
    visitor = _visitor_product_probe()
    windows = _windows_probe(python312)
    local_docker = _docker_probe()
    trusted_actions = _trusted_actions_probe()
    prerequisites = {name: _is_ancestor(commit) for name, commit in MERGED_PREREQUISITES.items()}
    blockers = []
    if windows.get("status") != "pass":
        blockers.append("windows_clean_start_restart_not_passed")
    if trusted_actions.get("status") != "pass":
        blockers.append("trusted_clean_runner_evidence_not_passed")
    if not visitor.get("published_document_retrieved"):
        blockers.append("published_document_not_retrievable_via_product_visitor_route")
    if not visitor.get("same_session_five_turn"):
        blockers.append("five_turn_product_http_e2e_not_passed")
    offline_green = (
        full.get("status") == "pass"
        and relevant.get("status") == "pass"
        and seed.get("status") == "pass"
        and all(prerequisites.values())
        and _is_ancestor(BASELINE_MAIN_SHA)
        and all(value for key, value in runtime.items() if isinstance(value, bool) and key not in {
            "visitor_retrieval_after_publish"
        })
    )
    if not offline_green:
        blockers.append("offline_regression_not_passed")
    report: dict[str, Any] = {
        "status": "blocked" if blockers else "pass",
        "report_schema_version": "d5-05-postmerge-gate.v2",
        "gate_id": "D5-GATE-05",
        "owner": "A",
        "baseline_main_sha": BASELINE_MAIN_SHA,
        "evaluated_source": source_binding,
        "merged_prerequisites": prerequisites,
        "offline_regression": {"full_unit": full, "d4_d5_relevant": relevant, "seed": seed,
                               "all_green": offline_green},
        "runtime_persistence": runtime,
        "visitor_product_e2e": visitor,
        "deployment": {
            "windows_local_online": windows,
            "local_docker_observation": local_docker,
            "trusted_clean_runners": trusted_actions,
            "docker_compose": {
                "status": "pass" if trusted_actions.get("status") == "pass" else "fail",
                "evidence_source": "trusted_main_push_actions",
                "local_cli_required": False,
            },
        },
        "five_turn_evidence": {
            "d4_public_offline_fixture_covered_by_relevant_suite": relevant.get("status") == "pass",
            "d5_product_http_five_turn": "pass" if visitor.get("same_session_five_turn") else "fail",
            "actual_http": visitor.get("actual_http", False),
            "turns_completed": visitor.get("turns_completed", 0),
            "clarification_round_trip": visitor.get("clarification_round_trip", False),
        },
        "gate_decision": {
            "d5_05_complete": not blockers and offline_green,
            "d5_06_admitted": not blockers and offline_green,
            "blockers": blockers,
            "allowed_parallel_work": [
                "D5-06-C test_harness_and_browser_security_preparation",
                "D5-06-B backend_security_and_performance_review_preparation",
                "D4-08 public_canary_prerequisite_preparation",
            ],
        },
        "boundaries": {
            "model": "qwen3.7-flash",
            "provider": "bailian_beijing",
            "model_api_calls": 0,
            "network_evaluation_calls": 0,
            "real_credential_files_read": 0,
            "formal_evaluation_validation_cases_read": 0,
            "seed_validation_assets_checked_by_validator": seed.get("validation_cases_checked", 0),
            "hidden_test_cases_read": 0,
            "estimated_cost_cny": 0.0,
            "localhost_http_only": True,
        },
        "not_proven": [
            "product_release_readiness", "real_model_online_behavior", "full_browser_matrix",
            "business_accuracy", "generalization", "empirical_calibration", "innovation_claim",
        ],
    }
    report["stable_sha256"] = _canonical_hash(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 D5-05 合并后 A 侧部署 Gate")
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--skip-tests", action="store_true", help="仅用于调试，正式报告禁止使用")
    parser.add_argument("--python312", type=Path)
    parser.add_argument("--evaluated-commit", help="新建报告时绑定的已提交源码 revision；默认当前 HEAD")
    args = parser.parse_args(argv)
    if args.skip_tests and (args.check or args.report.resolve() == REPORT_PATH.resolve()):
        print(json.dumps({"status": "fail", "error": "skip_tests_forbidden_for_committed_report"}))
        return 2
    if args.check and args.evaluated_commit:
        print(json.dumps({"status": "fail", "error": "evaluated_commit_forbidden_in_check"}))
        return 2
    committed: dict[str, Any] | None = None
    selected_commit = args.evaluated_commit
    try:
        if args.check:
            if not args.report.is_file():
                raise GateProbeError("committed_report_missing")
            loaded = json.loads(args.report.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise GateProbeError("committed_report_invalid")
            committed = loaded
            source = committed.get("evaluated_source")
            if not isinstance(source, dict) or not isinstance(source.get("evaluated_commit_sha"), str):
                raise GateProbeError("committed_source_binding_missing")
            selected_commit = source["evaluated_commit_sha"]
        source_binding = _verify_evaluated_source(selected_commit)
        report = build_report(
            run_tests=not args.skip_tests,
            python312=args.python312,
            evaluated_commit=selected_commit,
            evaluated_source=source_binding,
        )
    except (GateProbeError, OSError, json.JSONDecodeError) as exc:
        code = str(exc) if isinstance(exc, GateProbeError) else "committed_report_invalid"
        print(json.dumps({"status": "fail", "error": code}, ensure_ascii=False))
        return 1
    if args.check:
        if committed != report:
            print(json.dumps({"status": "fail", "error": "committed_report_drifted",
                              "rebuilt_stable_sha256": report["stable_sha256"]}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps({"status": "pass", "stable_sha256": report["stable_sha256"]}, ensure_ascii=False))
        return 0
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"pass", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_report", "_canonical_hash", "_docker_probe", "_source_manifest_for_commit",
    "_trusted_actions_probe", "_verify_evaluated_source", "_visitor_product_probe", "_windows_probe"
]
