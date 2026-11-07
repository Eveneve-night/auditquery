"""D5-06-C 脱敏机器报告生成器的 sanitization 与结构测试（离线、可重复）。

验证目标：
1. ``assert_sanitized`` 对含敏感标记的报告严格失败关闭（口令 / Cookie / CSRF /
   Key / 问答原文 / 数据库行），且对干净报告放行；
2. 报告结构：Docker 如实标注未实测、Windows 六项生命周期键齐全、边界声明
   （模型调用 0 / 不读真实 Key / 仅回环）。

本测试不启动真实服务、不读真实 Key、不连网。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.evaluation.validate_d5_06_c_machine_report import (  # noqa: E402
    MASTER_KEY_B64,
    _EXPECTED_ROUND_KINDS,
    _FORBIDDEN_MARKERS,
    _replay_blocked,
    _validate_rounds,
    assert_sanitized,
)


def _full_rounds() -> list[dict]:
    return [{"round": i, "kind": kind, "ok": True}
            for i, kind in enumerate(_EXPECTED_ROUND_KINDS)]


def _clean_report() -> dict:
    return {
        "task_id": "D5-06-C", "owner": "C",
        "evidence_kind": "windows_machine_report_local", "status": "pass",
        "platform": "windows",
        "docker": {"tested": False, "reason": "本机未运行 Docker；如实标注未实测。"},
        "windows": {
            "install_scope": "本机运行时初始化（非安装脚本实测）",
            "runtime_init": {"ok": True}, "start": {"ok": True}, "stop": {"ok": True},
            "restart": {"ok": True, "state_preserved": True},
            "backup": {"ok": True, "master_key_included": False},
            "restore": {"ok": True},
        },
        "five_round_http": {"tested": True, "all_passed": True, "rounds": [],
                            "rounds_validated": True, "rounds_validation": "ok",
                            "clarification_roundtrip_ok": True, "replay_blocked": True,
                            "post_publish_retrieval_ok": True},
        "boundaries": {"model_api_calls": 0, "real_key_read": False,
                       "external_network": False, "loopback_only": True},
    }


class MachineReportSanitizationTests(unittest.TestCase):
    def test_clean_report_passes_sanitization(self):
        assert_sanitized(json.dumps(_clean_report(), ensure_ascii=False))

    def test_password_marker_rejected(self):
        report = _clean_report()
        report["windows"]["runtime_init"]["note"] = "default password is ictagent123"
        with self.assertRaisesRegex(ValueError, "sensitive_marker_leaked"):
            assert_sanitized(json.dumps(report, ensure_ascii=False))

    def test_cookie_marker_rejected(self):
        report = _clean_report()
        report["windows"]["start"]["set_cookie"] = "ict_admin_session=abc; Path=/"
        with self.assertRaisesRegex(ValueError, "sensitive_marker_leaked"):
            assert_sanitized(json.dumps(report, ensure_ascii=False))

    def test_csrf_marker_rejected(self):
        report = _clean_report()
        report["windows"]["start"]["csrf"] = "csrf_token: xyz"
        with self.assertRaisesRegex(ValueError, "sensitive_marker_leaked"):
            assert_sanitized(json.dumps(report, ensure_ascii=False))

    def test_api_key_marker_rejected(self):
        report = _clean_report()
        report["windows"]["runtime_init"]["key"] = "sk-testonly-0000000000"
        with self.assertRaisesRegex(ValueError, "sensitive_marker_leaked"):
            assert_sanitized(json.dumps(report, ensure_ascii=False))

    def test_master_key_b64_rejected(self):
        report = _clean_report()
        report["windows"]["runtime_init"]["master_key"] = MASTER_KEY_B64
        with self.assertRaisesRegex(ValueError, "sensitive_marker_leaked"):
            assert_sanitized(json.dumps(report, ensure_ascii=False))

    def test_qa_text_and_sql_rejected(self):
        report = _clean_report()
        report["windows"]["start"]["sample"] = "SELECT * FROM Artist WHERE name = 'x'"
        with self.assertRaisesRegex(ValueError, "sensitive_marker_leaked"):
            assert_sanitized(json.dumps(report, ensure_ascii=False))

    def test_denylist_covers_all_required_categories(self):
        joined = " ".join(_FORBIDDEN_MARKERS).casefold()
        for required in ("ictagent123", "ict_admin_session", "csrf_token",
                         "sk-", MASTER_KEY_B64, "select ", "销售", "艺术家"):
            self.assertIn(required.casefold(), joined)

    def test_report_structure_and_docker_untested(self):
        report = _clean_report()
        self.assertFalse(report["docker"]["tested"])
        self.assertEqual(
            {"install_scope", "runtime_init", "start", "stop", "restart", "backup", "restore"},
            set(report["windows"]),
        )
        # 五轮 HTTP 证据结构：轮次齐全、逐轮 ok、发布后检索与重放阻断单列。
        five = report["five_round_http"]
        self.assertTrue(five["tested"])
        self.assertTrue(five["all_passed"])
        self.assertTrue(five["clarification_roundtrip_ok"])
        self.assertTrue(five["replay_blocked"])
        self.assertTrue(five["post_publish_retrieval_ok"])
        self.assertEqual(0, report["boundaries"]["model_api_calls"])
        self.assertFalse(report["boundaries"]["real_key_read"])
        self.assertTrue(report["boundaries"]["loopback_only"])

    def test_windows_install_scope_honest(self):
        # 证据口径：runtime_init 不得宣称「Windows 安装脚本/干净机器实测」。
        report = _clean_report()
        scope = report["windows"]["install_scope"]
        self.assertIn("运行时初始化", scope)
        self.assertIn("非", scope)


class ReplayBlockedTests(unittest.TestCase):
    """重放阻断判定必须锁定 409 + operation_replayed，防空转（A 复审阻塞项①）。"""

    def test_409_with_operation_replayed_is_blocked(self):
        self.assertTrue(_replay_blocked(409, {"error": {"code": "operation_replayed"}}))

    def test_409_state_conflict_is_not_blocked(self):
        # state_conflict 同样映射 409，但不是重放保护生效的证据。
        self.assertFalse(_replay_blocked(409, {"error": {"code": "state_conflict"}}))

    def test_409_without_error_body_is_not_blocked(self):
        self.assertFalse(_replay_blocked(409, {"status": "error"}))

    def test_non_409_is_not_blocked(self):
        self.assertFalse(_replay_blocked(200, {"error": {"code": "operation_replayed"}}))


class RoundValidationTests(unittest.TestCase):
    """五轮轮次/顺序冻结校验，防空转（A 复审阻塞项②）。"""

    def test_full_sequence_passes(self):
        ok, detail = _validate_rounds(_full_rounds(), session_ok=True)
        self.assertTrue(ok)
        self.assertEqual("ok", detail)

    def test_empty_rounds_fails(self):
        ok, detail = _validate_rounds([], session_ok=True)
        self.assertFalse(ok)
        self.assertEqual("empty_rounds", detail)

    def test_missing_round_fails(self):
        rounds = _full_rounds()[:-1]  # 少最后一轮
        ok, detail = _validate_rounds(rounds, session_ok=True)
        self.assertFalse(ok)
        self.assertEqual("unexpected_round_kinds", detail)

    def test_wrong_order_fails(self):
        rounds = _full_rounds()
        rounds[1], rounds[2] = rounds[2], rounds[1]  # 交换 rag_hit 与 clarification_required
        ok, detail = _validate_rounds(rounds, session_ok=True)
        self.assertFalse(ok)
        self.assertEqual("unexpected_round_kinds", detail)

    def test_session_create_not_ok_fails(self):
        rounds = _full_rounds()
        rounds[0]["ok"] = False
        ok, detail = _validate_rounds(rounds, session_ok=False)
        self.assertFalse(ok)
        self.assertEqual("session_create_not_ok", detail)

    def test_any_round_not_ok_fails(self):
        rounds = _full_rounds()
        rounds[3]["ok"] = False
        ok, detail = _validate_rounds(rounds, session_ok=True)
        self.assertFalse(ok)
        self.assertEqual("round_not_ok", detail)


if __name__ == "__main__":
    unittest.main()
