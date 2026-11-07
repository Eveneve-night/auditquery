#!/usr/bin/env python3
"""D5-06-C 脱敏机器报告生成器（Windows 实测 + Docker 诚实未实测）。

完成条件④：产出「安装/停止/重启/备份恢复」的机器报告，且报告**不得**含
口令、Cookie、CSRF、Key、问题/回答原文或数据库行。

本脚本在**本机 Windows** 上真实执行一条产品生命周期：
  1. 安装（实例初始化 + 默认管理员 bootstrap + 建表）；
  2. 启动（subprocess 起 ``backend.product.server serve``，等 /health/ready）；
  3. 停止（terminate）；
  4. 重启（再次启动，验证 pre-published 文档仍可读，证明状态持久化）；
  5. 备份（``infra.product_snapshot.backup``，主密钥不入快照）；
  6. 恢复（``restore`` 到新空目录，snapshot.json 校验通过）。

Docker 未在本机运行，按用户决策**如实标注未实测**，不伪装成通过证据。

报告只保留布尔结果 / 状态码 / 安全枚举字符串，经 ``_assert_sanitized``
强校验（denylist 标记 + 结构自检）后才落盘；敏感内容零泄漏。不读取真实
Key、不调用模型、不连外网（仅 127.0.0.1 回环）。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SRC))
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d5-06-c-machine-report-v1.json"
MASTER_KEY = b"a" * 32  # 测试专用假密钥，绝不等于真实密钥。
MASTER_KEY_B64 = base64.b64encode(MASTER_KEY).decode("ascii")

# 报告绝不允许出现的敏感标记（denylist）。
# 覆盖：口令 / Cookie / CSRF / Key / 问答原文 / 数据库行。
_FORBIDDEN_MARKERS = (
    "ictagent123",            # 默认管理员口令
    "ict_admin_session",      # 管理员会话 Cookie 名
    "ict_visitor_session",    # 访客会话 Cookie 名
    "Set-Cookie",             # 任何 Cookie 下发头
    "csrf_token",             # CSRF 令牌字段
    "x-csrf-token",           # CSRF 头
    "sk-",                    # API Key 前缀
    "ICT_SECRET_MASTER_KEY",  # 主密钥环境变量名
    MASTER_KEY_B64,           # 主密钥 base64 值
    "password",               # 口令字段名
    "销售",                   # 问答原文（业务内容）
    "艺术家",                 # 问答原文（业务内容）
    "SELECT ",                # 数据库行 / SQL
    "INSERT ",                # 数据库行 / SQL
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assert_sanitized(report_text: str) -> None:
    """报告文本绝不含任何敏感标记；否则抛 ValueError（失败关闭）。"""
    if not isinstance(report_text, str):
        raise ValueError("report_not_text")
    lowered = report_text.casefold()
    for marker in _FORBIDDEN_MARKERS:
        if marker.casefold() in lowered:
            raise ValueError(f"sensitive_marker_leaked:{marker}")


def _reserve_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def _wait_ready(process: subprocess.Popen, port: int, timeout: float = 30.0) -> bool:
    import urllib.request
    import urllib.error

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=1.0) as resp:
                if resp.status == 200 and json.loads(resp.read().decode("utf-8")).get("status") == "ready":
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.2)
    return False


def _start_server(instance: Path, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.pop("ICT_MASTER_KEY_FILE", None)
    env["ICT_SECRET_MASTER_KEY_B64"] = MASTER_KEY_B64
    env["PYTHONPATH"] = str(SRC)
    return subprocess.Popen(
        [sys.executable, "-m", "backend.product.server", "serve",
         "--instance", str(instance), "--mode", "online", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _stop_server(process: subprocess.Popen) -> bool:
    process.terminate()
    try:
        process.wait(timeout=10)
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        return False


def _publish_fixture(instance: Path) -> str:
    """在实例内上传并发布一份合法 PDF，返回 document_id（用于重启持久化验证）。"""
    from backend.product.document_store import DocumentAuditContext
    from backend.product.server import ProductRuntime

    runtime = ProductRuntime(instance, mode="online", master_key=MASTER_KEY)
    pdf = (PROJECT_ROOT / "data" / "raw" / "pdf" / "sales_playbook.pdf").read_bytes()
    status = runtime.document_app.document_service.upload(
        filename="sales_playbook.pdf", content_type="application/pdf",
        declared_bytes=len(pdf), content_sha256=_sha256(pdf),
        file_bytes=pdf, audit=DocumentAuditContext("req.machine", "sess.machine", "machine_report"),
    )
    runtime.document_app.document_service.publish(
        status.document_id, audit=DocumentAuditContext("req.machine.pub", "sess.machine", "machine_report")
    )
    return status.document_id


def _document_present(instance: Path, document_id: str) -> bool:
    from backend.product.server import ProductRuntime

    runtime = ProductRuntime(instance, mode="online", master_key=MASTER_KEY)
    try:
        runtime.document_app.document_service.read(document_id)
        return True
    except Exception:
        return False


def _publish_second_fixture(instance: Path) -> str:
    """运行中期再发布一份不同 hash 的 PDF，供「发布后访客检索」验证。"""
    from backend.product.document_store import DocumentAuditContext
    from backend.product.server import ProductRuntime

    runtime = ProductRuntime(instance, mode="online", master_key=MASTER_KEY)
    pdf = (PROJECT_ROOT / "data" / "raw" / "pdf" / "customer_success.pdf").read_bytes()
    status = runtime.document_app.document_service.upload(
        filename="customer_success.pdf", content_type="application/pdf",
        declared_bytes=len(pdf), content_sha256=_sha256(pdf),
        file_bytes=pdf, audit=DocumentAuditContext("req.machine.2", "sess.machine", "machine_report"),
    )
    runtime.document_app.document_service.publish(
        status.document_id, audit=DocumentAuditContext("req.machine.2.pub", "sess.machine", "machine_report")
    )
    return status.document_id


def _http_post_json(port: int, path: str, payload: dict, cookie: str | None = None):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        connection.request("POST", path, json.dumps(payload).encode("utf-8"), headers)
        response = connection.getresponse()
        raw_cookie = response.getheader("Set-Cookie")
        body = json.loads(response.read().decode("utf-8"))
        return response.status, raw_cookie, body
    finally:
        connection.close()


def _envelope(request_id: str, **extra) -> dict:
    return {"api_version": "3.0", "contract_version": "1.0.0", "request_id": request_id, **extra}


# 五轮证据的精确轮次顺序（冻结清单）。任何缺失 / 错序 / 空列表即失败（防空转）。
_EXPECTED_ROUND_KINDS = (
    "session_create",
    "rag_hit",
    "clarification_required",
    "clarification_submit",
    "rag_after_clarification",
    "no_evidence_not_forged",
    "post_publish_retrieval",
    "clarification_replay_blocked",
)


def _replay_blocked(status: int, body: dict) -> bool:
    """重放阻断判定：必须同时命中 HTTP 409 与稳定错误码 ``operation_replayed``。

    ``operation_replayed`` 与 ``state_conflict`` 都映射 409，仅凭 409 无法证明
    真正重放保护生效；任何非 operation_replayed 的 409（如 state_conflict）不算阻断。
    """
    return status == 409 and (body.get("error") or {}).get("code") == "operation_replayed"


def _validate_rounds(rounds: list[dict], session_ok: bool) -> tuple[bool, str]:
    """冻结五轮证据的轮次/顺序/完整性；任一缺失即失败（防空转）。

    校验：轮次非空、kind 顺序精确匹配冻结清单、round 序号连续、
    session_create 成功、逐轮 ok。返回 (通过, 详情枚举)。
    """
    if not rounds:
        return False, "empty_rounds"
    kinds = tuple(r.get("kind") for r in rounds)
    if kinds != _EXPECTED_ROUND_KINDS:
        return False, "unexpected_round_kinds"
    if [r.get("round") for r in rounds] != list(range(len(_EXPECTED_ROUND_KINDS))):
        return False, "round_index_mismatch"
    if not session_ok:
        return False, "session_create_not_ok"
    if not all(r.get("ok") for r in rounds):
        return False, "round_not_ok"
    return True, "ok"


def _five_round_http_evidence(port: int, instance: Path) -> dict:
    """真实 HTTP 五轮问答/澄清 + 发布后检索证据。

    时序：轮 1-5 仅一份 published 文档；轮 6 之前**会话中途**再发布第二份
    不同 hash 的 PDF，验证发布后访客立即可检索（不重启服务）。

    只记录布尔 / HTTP 状态码 / 响应枚举 / 计数，**不记录**问题与回答原文
    （报告脱敏由 ``assert_sanitized`` 兜底）。问题集合与
    ``tests/unit/test_d5_visitor_rag_http_e2e.py::test_five_turns_same_session_over_http``
    一致，这里逐轮留痕。
    """
    rounds: list[dict] = []

    status, set_cookie, body = _http_post_json(port, "/api/v3/sessions", _envelope("req.mr.create"))
    session_ok = status == 200 and bool(body.get("session_id")) and bool(set_cookie)
    cookie = set_cookie.split(";", 1)[0] if set_cookie else None
    sid = body.get("session_id")
    rounds.append({"round": 0, "kind": "session_create", "http_status": status, "ok": session_ok})

    turn = revision = 0

    def _query(request_id: str, question: str) -> tuple[int, dict]:
        return _http_post_json(port, "/api/v3/query", _envelope(
            request_id, session_id=sid, turn_id=turn, state_revision=revision, question=question,
        ), cookie)

    def _round(index: int, kind: str, http_status: int, body: dict, extra: dict | None = None) -> None:
        record = {"round": index, "kind": kind, "http_status": http_status,
                  "sent_turn": turn, "sent_revision": revision,
                  "response_status": body.get("status"),
                  "error_code": (body.get("error") or {}).get("code"),
                  "response_revision": body.get("state_revision")}
        if extra:
            record.update(extra)
        rounds.append(record)

    # 轮 1：RAG 命中（published 文档）。
    status, _, body = _query("req.mr.t1", "销售流程")
    payload = body.get("payload", {})
    citations = payload.get("citations", [])
    r1_ok = (status == 200 and body.get("status") == "success"
             and payload.get("route") == "rag" and bool(citations)
             and all(c.get("document_id") and str(c.get("evidence_id", "")).startswith(c["document_id"] + ".p")
                     for c in citations))
    _round(1, "rag_hit", status, body, {"route": payload.get("route"), "citations": len(citations), "ok": r1_ok})
    revision, turn = body.get("state_revision", revision), turn + 1

    # 轮 2：SQL 意图 → 澄清（离线失败关闭，不伪造答案）。
    # This probe requires a genuinely missing metric; complete count questions
    # are now resolved before asking a user to repeat their explicit intent.
    status, _, body = _query("req.mr.t2", "艺术家")
    payload = body.get("payload", {})
    pending = payload.get("pending_question") or {}
    r2_ok = (status == 200 and body.get("status") == "clarification_required"
             and payload.get("route") == "clarification"
             and not payload.get("citations") and bool(pending.get("options")))
    _round(2, "clarification_required", status, body, {"route": payload.get("route"), "options": len(pending.get("options", [])), "ok": r2_ok})
    revision, turn = body.get("state_revision", revision), turn + 1

    # 轮 3：澄清提交（单值单证据 + 六绑定字段原样回传）→ 仍澄清（多槽位）。
    option = (pending.get("options") or [{}])[0]
    status, _, body = _http_post_json(port, "/api/v3/clarifications", _envelope(
        "req.mr.t2", answer_id="ans.mr.turn3", session_id=sid,
        turn_id=pending.get("turn_id"), state_revision=pending.get("state_revision"),
        question_id=pending.get("question_id"), slot_id=pending.get("slot_id"),
        values=[option.get("value")], evidence_refs=[option.get("evidence_ref")],
    ), cookie)
    r3_ok = (status == 200 and body.get("status") == "clarification_required"
             and body.get("payload", {}).get("route") == "clarification")
    _round(3, "clarification_submit", status, body, {"route": body.get("payload", {}).get("route"), "ok": r3_ok})
    revision, turn = body.get("state_revision", revision), turn + 1

    # 轮 4：澄清后同一会话仍可回到 RAG 路由。
    status, _, body = _query("req.mr.t4", "月度经营报表的截止时间")
    payload = body.get("payload", {})
    r4_ok = (status == 200 and body.get("status") == "success"
             and payload.get("route") == "rag" and bool(payload.get("citations")))
    _round(4, "rag_after_clarification", status, body, {"route": payload.get("route"), "citations": len(payload.get("citations", [])), "ok": r4_ok})
    revision, turn = body.get("state_revision", revision), turn + 1

    # 轮 5：无证据问题 → 非 success 且零伪造 citations。
    status, _, body = _query("req.mr.t5", "区块链共识算法")
    payload = body.get("payload", {})
    r5_ok = (status == 200 and body.get("status") != "success"
             and not payload.get("citations") and payload.get("route") != "rag")
    _round(5, "no_evidence_not_forged", status, body, {"citations": len(payload.get("citations", [])), "ok": r5_ok})
    revision, turn = body.get("state_revision", revision), turn + 1

    # 轮 6：发布后访客检索——会话中途发布新文档（不重启服务），对访客立即可见。
    second_document_id = _publish_second_fixture(instance)
    status, _, body = _query("req.mr.t6", "客户成功案例")
    payload = body.get("payload", {})
    citations = payload.get("citations", [])
    hit_second = any(c.get("document_id") == second_document_id for c in citations)
    r6_ok = (status == 200 and body.get("status") == "success"
             and payload.get("route") == "rag" and bool(citations) and hit_second)
    _round(6, "post_publish_retrieval", status, body, {"route": payload.get("route"), "citations": len(citations), "hit_published_document": hit_second, "ok": r6_ok})

    # 澄清重放阻断：同 answer_id 重复提交 → 409。
    status, _, body = _http_post_json(port, "/api/v3/clarifications", _envelope(
        "req.mr.t2.replay", answer_id="ans.mr.turn3", session_id=sid,
        turn_id=pending.get("turn_id"), state_revision=pending.get("state_revision"),
        question_id=pending.get("question_id"), slot_id=pending.get("slot_id"),
        values=[option.get("value")], evidence_refs=[option.get("evidence_ref")],
    ), cookie)
    replay_blocked = _replay_blocked(status, body)
    _round(7, "clarification_replay_blocked", status, body, {"ok": replay_blocked})

    rounds_validated, rounds_validation = _validate_rounds(rounds, session_ok)
    return {
        "tested": True,
        "transport": "real_http_loopback",
        "session_create_ok": session_ok,
        "rounds": rounds,
        "rounds_validated": rounds_validated,
        "rounds_validation": rounds_validation,
        "clarification_roundtrip_ok": bool(r2_ok and r3_ok),
        "replay_blocked": replay_blocked,
        "post_publish_retrieval_ok": bool(r6_ok),
        "all_passed": rounds_validated,
    }


def run_machine_report() -> dict:
    import shutil

    from infra.product_snapshot import backup, restore

    base = Path(tempfile.mkdtemp(prefix="d5-06-c-machine-"))
    try:
        instance = base / "instance"
        snapshot = base / "snapshot"
        restored = base / "restored"

        # 1. 运行时初始化（证据口径：本机 ProductRuntime 初始化 + 默认管理员 bootstrap，
        #    **非** Windows 安装脚本、**非**另一台干净机器实测——不扩大证据口径）。
        from backend.product.server import ProductRuntime
        runtime = ProductRuntime(instance, mode="online", master_key=MASTER_KEY)
        runtime_init_ok = runtime.auth.store.read_state()["username"] == "admin"
        document_id = _publish_fixture(instance)

        # 2. 启动。
        port = _reserve_port()
        process = _start_server(instance, port)
        start_ok = _wait_ready(process, port)

        # 3. 停止。
        stop_ok = _stop_server(process)

        # 4. 重启：重新启动，验证 pre-published 文档仍可读（状态持久化）。
        port2 = _reserve_port()
        process2 = _start_server(instance, port2)
        restart_ok = _wait_ready(process2, port2)
        state_preserved = restart_ok and _document_present(instance, document_id)
        _stop_server(process2)

        # 4.5 真实 HTTP 五轮问答/澄清 + 澄清重放阻断 + 会话中途发布后访客检索
        #     （第二份 PDF 在轮 6 前发布，不在生命周期阶段预置，避免改变检索语料）。
        port3 = _reserve_port()
        process3 = _start_server(instance, port3)
        five_round_ready = _wait_ready(process3, port3)
        try:
            five_round = (_five_round_http_evidence(port3, instance)
                          if five_round_ready else {"tested": False, "all_passed": False,
                                                    "rounds": [], "rounds_validated": False,
                                                    "rounds_validation": "server_not_ready",
                                                    "clarification_roundtrip_ok": False,
                                                    "replay_blocked": False,
                                                    "post_publish_retrieval_ok": False})
        finally:
            _stop_server(process3)

        # 5. 备份：主密钥不入快照。
        backup_ok = False
        try:
            backup(instance, snapshot)
            manifest = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
            backup_ok = manifest["master_key_included"] is False and manifest["format"] == 2
        except (ValueError, OSError, KeyError, json.JSONDecodeError):
            backup_ok = False

        # 6. 恢复：还原到新空目录（restore 不复制 snapshot.json，只还原 DB + documents），
        #    用还原后的实例重新打开验证管理员状态完整。
        restore_ok = False
        try:
            restore(snapshot, restored)
            from backend.product.server import ProductRuntime as _RT
            reopened = _RT(restored, mode="online", master_key=MASTER_KEY)
            restore_ok = reopened.auth.store.read_state()["username"] == "admin"
        except (ValueError, OSError):
            restore_ok = False

        return {
            "task_id": "D5-06-C",
            "owner": "C",
            "evidence_kind": "windows_machine_report_local",
            "status": "pass" if all((runtime_init_ok, start_ok, stop_ok, restart_ok, state_preserved,
                                     backup_ok, restore_ok, five_round["all_passed"])) else "fail",
            "platform": "windows",
            "docker": {"tested": False,
                       "reason": "本机未运行 Docker；按用户决策如实标注未实测，不作为通过证据。"},
            "windows": {
                "install_scope": "本机运行时初始化（ProductRuntime + 默认管理员 bootstrap + 建表）；"
                                 "非 Windows 安装脚本实测，非另一台干净机器实测。",
                "runtime_init": {"ok": runtime_init_ok},
                "start": {"ok": start_ok},
                "stop": {"ok": stop_ok},
                "restart": {"ok": restart_ok, "state_preserved": state_preserved},
                "backup": {"ok": backup_ok, "master_key_included": False},
                "restore": {"ok": restore_ok},
            },
            "five_round_http": five_round,
            "boundaries": {
                "model_api_calls": 0,
                "real_key_read": False,
                "external_network": False,
                "loopback_only": True,
            },
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="D5-06-C 脱敏机器报告生成器")
    parser.add_argument("--skip-lifecycle", action="store_true",
                        help="仅生成结构自检样例（不启动真实服务，供测试）")
    args = parser.parse_args()

    if args.skip_lifecycle:
        report = {
            "task_id": "D5-06-C", "owner": "C",
            "evidence_kind": "windows_machine_report_local", "status": "fail",
            "platform": "windows",
            "docker": {"tested": False, "reason": "未实测"},
            "windows": {"install_scope": "本机运行时初始化（非安装脚本实测）",
                        "runtime_init": {"ok": False}, "start": {"ok": False}, "stop": {"ok": False},
                        "restart": {"ok": False, "state_preserved": False},
                        "backup": {"ok": False, "master_key_included": False},
                        "restore": {"ok": False}},
            "five_round_http": {"tested": False, "all_passed": False, "rounds": [],
                                "rounds_validated": False, "rounds_validation": "not_run",
                                "clarification_roundtrip_ok": False, "replay_blocked": False,
                                "post_publish_retrieval_ok": False},
            "boundaries": {"model_api_calls": 0, "real_key_read": False,
                           "external_network": False, "loopback_only": True},
        }
    else:
        report = run_machine_report()

    report_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    assert_sanitized(report_text)
    report["sanitization"] = {"passed": True, "markers_checked": len(_FORBIDDEN_MARKERS)}
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
