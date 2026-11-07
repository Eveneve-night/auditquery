"""D4-02 单表 NL2SQL 安全执行纵切离线测试。

覆盖：
1. SQL 安全网关：放行合法单表查询，拦截写操作/多表/子查询/危险函数/ATTACH/PRAGMA/
   多语句/注释，参数化与限行校验。
2. 只读执行器：放行查询返回结果、写操作被拦截、限行截断、指令数上限中断。
3. 10 条典型单表查询纵切：安全网关放行 + 只读执行返回预期行数。

仅离线：网络 0，模型 API 0，不读评测集答案字段（10 条样本为独立构造的典型单表查询）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backend.nl2sql.readonly_executor import ReadonlyExecutor  # noqa: E402
from backend.nl2sql.sql_safety_gate import SafetyDecision, validate_candidate  # noqa: E402

SAMPLES_PATH = PROJECT_ROOT / "tests" / "fixtures" / "d4-02" / "single-table-queries.json"
MALICIOUS_PATH = PROJECT_ROOT / "tests" / "fixtures" / "d4-02" / "malicious-sql-samples.json"


def _load_samples() -> list[dict]:
    return json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))


def _load_malicious() -> list[dict]:
    return json.loads(MALICIOUS_PATH.read_text(encoding="utf-8"))


class SQLSafetyGateTest(unittest.TestCase):
    def test_rejects_implicit_self_join(self) -> None:
        for sql in (
            "SELECT a.ArtistId FROM Artist a, Artist b",
            "SELECT COUNT(*) FROM Artist a, Artist b",
            "SELECT a.ArtistId FROM Artist a, Artist b WHERE a.ArtistId=b.ArtistId",
        ):
            with self.subTest(sql=sql):
                self.assertFalse(validate_candidate(sql, ["Artist"], []).approved)

    def test_rejects_compound_single_table_queries(self) -> None:
        for operator in ("EXCEPT", "INTERSECT"):
            sql = f"SELECT ArtistId FROM Artist {operator} SELECT ArtistId FROM Artist"
            with self.subTest(operator=operator):
                self.assertFalse(validate_candidate(sql, ["Artist"], []).approved)

    def test_allows_valid_single_table_queries(self) -> None:
        for sample in _load_samples():
            with self.subTest(id=sample["id"]):
                decision = validate_candidate(sample["sql"], sample["tables"], sample["params"])
                self.assertTrue(decision.approved, f"{sample['id']} 应放行: {decision.reason}")
                self.assertIsNotNone(decision.execution_sql)

    def test_rejects_write_and_ddl(self) -> None:
        malicious = [
            "DELETE FROM Artist",
            "INSERT INTO Artist (ArtistId, Name) VALUES (1, 'x')",
            "UPDATE Artist SET Name = 'x'",
            "DROP TABLE Artist",
            "CREATE TABLE Hack (id INTEGER)",
            "ALTER TABLE Artist ADD COLUMN x TEXT",
        ]
        for sql in malicious:
            with self.subTest(sql=sql):
                decision = validate_candidate(sql, ["Artist"], [])
                self.assertFalse(decision.approved, f"应拒绝写操作: {sql}")

    def test_rejects_join_and_subquery(self) -> None:
        malicious = [
            "SELECT * FROM Artist a JOIN Album b ON a.ArtistId = b.ArtistId",
            "SELECT * FROM (SELECT ArtistId FROM Artist)",
            "SELECT * FROM Track WHERE AlbumId IN (SELECT AlbumId FROM Album)",
        ]
        for sql in malicious:
            with self.subTest(sql=sql):
                decision = validate_candidate(sql, ["Artist"], [])
                self.assertFalse(decision.approved, f"应拒绝 JOIN/子查询: {sql}")

    def test_rejects_dangerous_functions(self) -> None:
        for sql in (
            "SELECT load_extension('x')",
            "SELECT sqlite_version()",
            "SELECT random() FROM Artist",
            "SELECT hex(ArtistId) FROM Artist",
        ):
            with self.subTest(sql=sql):
                decision = validate_candidate(sql, ["Artist"], [])
                self.assertFalse(decision.approved, f"应拒绝危险函数: {sql}")

    def test_rejects_attach_pragma_and_multi_statement(self) -> None:
        malicious = [
            "ATTACH DATABASE 'x.db' AS other",
            "PRAGMA table_info(Artist)",
            "SELECT * FROM Artist; DROP TABLE Artist",
            "SELECT * FROM Artist -- comment",
        ]
        for sql in malicious:
            with self.subTest(sql=sql):
                decision = validate_candidate(sql, ["Artist"], [])
                self.assertFalse(decision.approved, f"应拒绝: {sql}")

    def test_rejects_wrong_table_declaration(self) -> None:
        # SQL 访问 Artist，却声明 Track。
        decision = validate_candidate("SELECT COUNT(*) FROM Artist", ["Track"], [])
        self.assertFalse(decision.approved)
        self.assertIn("表", decision.reason or "")

    def test_rejects_multi_table_declaration(self) -> None:
        decision = validate_candidate("SELECT COUNT(*) FROM Artist", ["Artist", "Album"], [])
        self.assertFalse(decision.approved)

    def test_param_count_must_match(self) -> None:
        decision = validate_candidate("SELECT COUNT(*) FROM Customer WHERE Country = ?", ["Customer"], [])
        self.assertFalse(decision.approved)
        self.assertIn("参数", decision.reason or "")

    def test_all_malicious_fixtures_blocked(self) -> None:
        """恶意样本库（≥30 条，覆盖写操作/JOIN/子查询/危险函数/系统操作/语句结构/越界）
        必须全部被安全网关拦截。"""
        malicious = _load_malicious()
        self.assertGreaterEqual(len(malicious), 30, "恶意样本应 ≥30 条（REQ-S01）")
        for sample in malicious:
            with self.subTest(id=sample["id"]):
                decision = validate_candidate(sample["sql"], sample["tables"], sample["params"])
                self.assertFalse(decision.approved, f"{sample['id']} 应被拦截")

    def test_row_limit_injected_when_absent(self) -> None:
        decision = validate_candidate("SELECT TrackId, Name FROM Track", ["Track"], [])
        self.assertTrue(decision.approved)
        self.assertTrue(decision.execution_sql.rstrip().endswith("LIMIT 100"))

    def test_excessive_limit_rejected(self) -> None:
        decision = validate_candidate("SELECT * FROM Track LIMIT 500", ["Track"], [], max_rows=100)
        self.assertFalse(decision.approved)
        self.assertIn("上限", decision.reason or "")

    def test_rejects_semicolon_and_non_literal_limit(self) -> None:
        for sql, params in (
            ("SELECT ArtistId FROM Artist;", []),
            ("SELECT ArtistId FROM Artist LIMIT ?", [5]),
            ("SELECT ArtistId FROM Artist LIMIT 5, 10", []),
        ):
            with self.subTest(sql=sql):
                self.assertFalse(validate_candidate(sql, ["Artist"], params).approved)

    def test_keywords_inside_literals_are_not_treated_as_syntax(self) -> None:
        decision = validate_candidate(
            "SELECT 'LIMIT 500; -- not syntax' AS note, ArtistId FROM Artist",
            ["Artist"],
            [],
        )
        self.assertTrue(decision.approved, decision.reason)

    def test_question_mark_inside_literal_is_not_a_parameter(self) -> None:
        decision = validate_candidate(
            "SELECT '?' AS marker, ArtistId FROM Artist WHERE ArtistId = ?",
            ["Artist"],
            [1],
        )
        self.assertTrue(decision.approved, decision.reason)


class ReadonlyExecutorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.executor = ReadonlyExecutor()

    def test_executes_approved_query(self) -> None:
        decision = validate_candidate("SELECT COUNT(*) AS c FROM Artist", ["Artist"], [])
        result = self.executor.execute(decision)
        self.assertEqual(("c",), result.columns)
        self.assertEqual(1, result.row_count)
        self.assertEqual(275, result.rows[0][0])
        self.assertFalse(result.truncated)

    def test_parameterized_query(self) -> None:
        decision = validate_candidate(
            "SELECT COUNT(*) AS c FROM Customer WHERE Country = ?",
            ["Customer"],
            ["USA"],
        )
        result = self.executor.execute(decision, ["USA"])
        self.assertEqual(13, result.rows[0][0])

    def test_raw_sql_cannot_bypass_safety_gate(self) -> None:
        with self.assertRaises(TypeError):
            self.executor.execute("SELECT sqlite_version()")  # type: ignore[arg-type]

    def test_rejected_or_forged_decision_is_blocked_at_executor_layer(self) -> None:
        rejected = validate_candidate("DELETE FROM Artist", ["Artist"], [])
        with self.assertRaises(ValueError):
            self.executor.execute(rejected)
        forged = SafetyDecision(
            approved=True,
            execution_sql=(
                "SELECT a.ArtistId FROM Artist a "
                "JOIN Album b ON a.ArtistId=b.ArtistId LIMIT 1"
            ),
            tables=("Artist",),
            row_limit=100,
        )
        with self.assertRaises(ValueError):
            self.executor.execute(forged)

    def test_row_truncation(self) -> None:
        executor = ReadonlyExecutor(max_rows=5)
        decision = validate_candidate(
            "SELECT TrackId FROM Track ORDER BY TrackId", ["Track"], []
        )
        result = executor.execute(decision)
        self.assertEqual(5, result.row_count)
        self.assertTrue(result.truncated)

    def test_forged_implicit_join_and_compound_decisions_are_rejected(self) -> None:
        for sql in (
            "SELECT a.ArtistId FROM Artist a, Artist b LIMIT 10",
            "SELECT ArtistId FROM Artist EXCEPT SELECT ArtistId FROM Artist LIMIT 10",
            "SELECT ArtistId FROM Artist INTERSECT SELECT ArtistId FROM Artist LIMIT 10",
        ):
            with self.subTest(sql=sql):
                decision = SafetyDecision(True, execution_sql=sql, tables=("Artist",), row_limit=100)
                with self.assertRaises(ValueError):
                    self.executor.execute(decision)

    def test_vm_step_limit_interrupts_runaway_query(self) -> None:
        # 极小指令数上限：正常查询也会被 progress handler 中断。
        executor = ReadonlyExecutor(max_vm_steps=20)
        decision = validate_candidate(
            "SELECT * FROM Invoice ORDER BY InvoiceId", ["Invoice"], []
        )
        with self.assertRaises(sqlite3.OperationalError):
            executor.execute(decision)

    def test_wall_clock_limit_interrupts_query(self) -> None:
        executor = ReadonlyExecutor(max_elapsed_ms=0)
        decision = validate_candidate(
            "SELECT * FROM Track ORDER BY Name", ["Track"], []
        )
        with self.assertRaises(sqlite3.OperationalError):
            executor.execute(decision)


class SingleTableSliceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.executor = ReadonlyExecutor()
        cls.samples = _load_samples()

    def test_all_samples_run_end_to_end(self) -> None:
        for sample in self.samples:
            with self.subTest(id=sample["id"]):
                decision = validate_candidate(sample["sql"], sample["tables"], sample["params"])
                self.assertTrue(decision.approved, decision.reason)
                result = self.executor.execute(decision, sample["params"])
                self.assertEqual(
                    sample["expected_rows"],
                    result.row_count,
                    f"{sample['id']} 返回行数不符",
                )
                self.assertTrue(result.columns, f"{sample['id']} 缺少列名")
                self.assertFalse(result.truncated, f"{sample['id']} 不应被截断")


if __name__ == "__main__":
    unittest.main()
