"""D4-03 Chinook 领域包 v1 组件测试（C 侧）。

覆盖范围：
1. 复用 A 冻结的 validate_domain_pack 全量纪律（schema、hash 链、悬空引用、环、答案字段）。
2. 与授权事实源的保真度（冻结 schema 文件、只读数据库）。
3. 防过拟合（金标 query / gold_sql 全文不得进入领域包；无密钥）。
4. 构建确定性（重建逐字节一致）与契约词汇覆盖。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = PROJECT_ROOT / "domain-packs" / "chinook" / "v1"
SCHEMA_PATH = PROJECT_ROOT / "data" / "schema" / "chinook-v1.4.5.schema.json"
DB_PATH = PROJECT_ROOT / "data" / "raw" / "chinook-v1.4.5" / "Chinook_Sqlite.sqlite"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-domain-pack-v1.json"
BUILDER_PATH = PROJECT_ROOT / "tools" / "rag" / "build_chinook_domain_pack.py"
VALIDATOR_PATH = PROJECT_ROOT / "tools" / "evaluation" / "validate_d4_evaluation_freeze.py"
GOLDEN_DIR = PROJECT_ROOT / "tests" / "goldens" / "model-eval" / "v1"

PACK_FILES = [
    "manifest.json",
    "concepts.json",
    "aliases.json",
    "slots.json",
    "schema-catalog.json",
    "relations.json",
    "value-evidence.json",
    "validation-rules.json",
    "LICENSE-or-SOURCE.md",
]
SECRET_PATTERNS = [
    r"api[_-]?key",
    r"apikey",
    r"\bsk-[A-Za-z0-9]{8,}\b",
    r"password",
    r"secret",
    r"BEGIN [A-Z ]*PRIVATE KEY",
    r"\baccess[_-]?token\b",
]


def sha256_lf(path: Path) -> str:
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_validator() -> object:
    spec = importlib.util.spec_from_file_location(
        "validate_d4_evaluation_freeze", VALIDATOR_PATH
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def read_goldens(filename: str) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for line in (GOLDEN_DIR / filename).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


class DomainPackComponentTest(unittest.TestCase):
    """Chinook 领域包 v1 的组件级校验。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = read_json(PACK_ROOT / "manifest.json")
        cls.artifacts = {
            "concepts": read_json(PACK_ROOT / "concepts.json"),
            "aliases": read_json(PACK_ROOT / "aliases.json"),
            "slots": read_json(PACK_ROOT / "slots.json"),
            "schema_catalog": read_json(PACK_ROOT / "schema-catalog.json"),
            "relations": read_json(PACK_ROOT / "relations.json"),
            "value_evidence": read_json(PACK_ROOT / "value-evidence.json"),
            "validation_rules": read_json(PACK_ROOT / "validation-rules.json"),
        }
        cls.pack_texts = {
            name: (PACK_ROOT / name).read_text(encoding="utf-8") for name in PACK_FILES
        }
        cls.validator = load_validator()

    def test_frozen_validator_passes_on_chinook_pack(self) -> None:
        """A 冻结的领域包校验纪律必须在本包上全绿。"""
        result = self.validator.validate_domain_pack(PACK_ROOT)
        self.assertEqual("chinook_music_store", result["domain_pack_id"])
        self.assertEqual(0, result["dangling_reference_count"])
        self.assertEqual(0, result["hash_mismatch_count"])
        self.assertEqual(0, result["forbidden_answer_field_count"])
        self.assertEqual(0, result["inheritance_cycle_count"])

    def test_manifest_hash_chain_matches_committed_files(self) -> None:
        files = self.manifest["files"]
        self.assertEqual(8, len(files))
        roles = [entry["role"] for entry in files]
        self.assertEqual(len(roles), len(set(roles)))
        for entry in files:
            with self.subTest(role=entry["role"]):
                self.assertEqual(sha256_lf(PACK_ROOT / entry["path"]), entry["sha256"])
                self.assertTrue(entry["send_to_model"] or entry["role"] == "license_notice")

    def test_rebuild_reproduces_committed_pack(self) -> None:
        """重建必须逐字节一致（created_at 固定、序列化参数固定）。"""
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, str(BUILDER_PATH), "--output-root", tmp],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(0, completed.returncode)
            for name in PACK_FILES:
                with self.subTest(file=name):
                    rebuilt = Path(tmp) / "domain-packs" / "chinook" / "v1" / name
                    self.assertTrue(rebuilt.is_file(), f"重建缺少 {name}")
                    self.assertEqual(sha256_lf(PACK_ROOT / name), sha256_lf(rebuilt))
            rebuilt_report = Path(tmp) / "tests" / "reports" / "d4-domain-pack-v1.json"
            self.assertEqual(sha256_lf(REPORT_PATH), sha256_lf(rebuilt_report))

    def test_schema_catalog_matches_frozen_schema_file(self) -> None:
        """目录对象必须与冻结 schema 文件的表/列/类型完全一致。"""
        schema = read_json(SCHEMA_PATH)
        objects = self.artifacts["schema_catalog"]["items"]
        # 列名在多表间会重名（如 Genre/MediaType/Playlist 都有 Name），必须按
        # 物理名 + 父对象联合索引，否则会拿错对象还假通过。
        by_object = {
            (item["physical_name"], item["parent_object_id"]): item for item in objects
        }
        expected_total = 1 + len(schema["tables"]) + sum(
            len(table["columns"]) for table in schema["tables"].values()
        )
        self.assertEqual(expected_total, len(objects))
        for table_name, table in schema["tables"].items():
            table_object = by_object[(table_name, "schema.chinook")]
            self.assertEqual("table", table_object["object_type"])
            self.assertEqual(
                [f"concept.{table_name.lower()}"], table_object["concept_refs"]
            )
            for column_name, column in table["columns"].items():
                column_object = by_object[
                    (column_name, f"schema.chinook.{table_name.lower()}")
                ]
                self.assertEqual("column", column_object["object_type"])
                self.assertEqual(column["type"], column_object["data_type"])
                self.assertEqual(
                    f"schema.chinook.{table_name.lower()}",
                    column_object["parent_object_id"],
                )

    def test_relations_match_frozen_relationships(self) -> None:
        """外键关系必须与冻结 schema 文件的关系集合一致。"""
        schema = read_json(SCHEMA_PATH)
        relations = self.artifacts["relations"]["items"]
        built = {
            (item["source_object_id"], item["target_object_id"]) for item in relations
        }
        expected: set[tuple[str, str]] = set()
        for relationship in schema["relationships"]:
            source_table, source_column = relationship["from"].split(".")
            target_table, target_column = relationship["to"].split(".")
            expected.add(
                (
                    f"schema.chinook.{source_table.lower()}.{source_column.lower()}",
                    f"schema.chinook.{target_table.lower()}.{target_column.lower()}",
                )
            )
        self.assertEqual(expected, built)
        self.assertEqual(len(expected), len(relations))

    def test_country_evidence_matches_database(self) -> None:
        """国家枚举必须与只读查询的实际国家集合一致（防 DB 版本漂移）。"""
        connection = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
        try:
            countries = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT Country FROM Customer ORDER BY Country"
                )
            ]
        finally:
            connection.close()
        evidence = {
            item["evidence_id"]: item
            for item in self.artifacts["value_evidence"]["items"]
        }
        self.assertEqual(countries, evidence["evidence.country_enum"]["payload"]["values"])

    def test_slot_vocabulary_covers_contract_slots(self) -> None:
        """领域包槽位必须覆盖 D3 冻结契约出现的槽位键名（prompt_dev 输入侧）。"""
        contract_slots: set[str] = set()
        for case in read_goldens("clarification.jsonl"):
            if case["split"] != "prompt_dev":
                continue
            contract_slots.update(case.get("context", {}).get("required_slots", []))
            contract_slots.update(case["input"].get("known_slots", {}).keys())
        self.assertTrue(contract_slots, "契约槽位集合不应为空")
        pack_slots = {
            item["slot_id"].removeprefix("slot.")
            for item in self.artifacts["slots"]["items"]
        }
        missing = sorted(contract_slots - pack_slots)
        self.assertEqual([], missing, f"领域包缺少契约槽位: {missing}")

    def test_no_golden_query_text_in_pack(self) -> None:
        """任何 split 的题干全文都不得出现在领域包中。"""
        for filename in sorted(p.name for p in GOLDEN_DIR.glob("*.jsonl")):
            for case in read_goldens(filename):
                query = case["input"].get("query")
                if not isinstance(query, str) or len(query) < 8:
                    continue
                for name, text in self.pack_texts.items():
                    with self.subTest(case=case["id"], file=name):
                        self.assertNotIn(query, text)

    def test_no_gold_sql_in_pack(self) -> None:
        """金标 SQL 全文不得出现在领域包中。"""
        for case in read_goldens("nl2sql.jsonl"):
            gold_sql = case["expected"].get("gold_sql")
            if not isinstance(gold_sql, str) or len(gold_sql) < 10:
                continue
            normalized = " ".join(gold_sql.split())
            for name, text in self.pack_texts.items():
                with self.subTest(case=case["id"], file=name):
                    self.assertNotIn(normalized, " ".join(text.split()))

    def test_pack_contains_no_secrets(self) -> None:
        for name, text in self.pack_texts.items():
            for pattern in SECRET_PATTERNS:
                with self.subTest(file=name, pattern=pattern):
                    self.assertIsNone(
                        re.search(pattern, text, flags=re.IGNORECASE),
                        f"{name} 命中敏感模式 {pattern}",
                    )

    def test_report_counts_match_pack(self) -> None:
        report = read_json(REPORT_PATH)
        counts = report["domain_pack"]["item_counts"]
        self.assertEqual(len(self.artifacts["concepts"]["items"]), counts["concepts"])
        self.assertEqual(len(self.artifacts["aliases"]["items"]), counts["aliases"])
        self.assertEqual(len(self.artifacts["slots"]["items"]), counts["slots"])
        self.assertEqual(
            len(self.artifacts["schema_catalog"]["items"]),
            counts["schema_catalog_objects"],
        )
        self.assertEqual(len(self.artifacts["relations"]["items"]), counts["relations"])
        self.assertEqual(
            len(self.artifacts["value_evidence"]["items"]), counts["value_evidence"]
        )
        self.assertEqual(
            len(self.artifacts["validation_rules"]["items"]),
            counts["validation_rules"],
        )
        self.assertEqual(sha256_lf(PACK_ROOT / "manifest.json"), report["domain_pack"]["manifest_sha256"])


if __name__ == "__main__":
    unittest.main()
