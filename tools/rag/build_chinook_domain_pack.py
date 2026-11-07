"""构建 Chinook 领域语义包 v1（D4-03 C 侧交付）。

数据来源（全部授权、离线、可复现）：
1. data/schema/chinook-v1.4.5.schema.json —— 表/列/类型/描述/别名/行数（冻结事实源）。
2. data/raw/chinook-v1.4.5/Chinook_Sqlite.sqlite（只读 URI）—— 值证据（国家枚举、发票年份范围）。
3. D3 冻结契约槽位词汇 —— configs/prompts/clarification/v1/system.md 的契约语义；
   槽位键名与取值令牌和 prompt_dev 输入侧（context.required_slots / input.known_slots）对齐。

数据边界（docs/23 防过拟合红线）：
- 不读取任何金标 expected / gold_sql / result 字段。
- validation / test split 内容零进入本包。
- 值证据仅含公开样例数据库的聚合值集合，不含客户个人数据。

确定性：created_at 为固定常量；JSON 序列化参数固定；重建输出逐字节一致
（tests/unit/test_d4_domain_pack.py::test_rebuild_reproduces_committed_pack 锁定）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = PROJECT_ROOT / "data" / "schema" / "chinook-v1.4.5.schema.json"
DB_PATH = PROJECT_ROOT / "data" / "raw" / "chinook-v1.4.5" / "Chinook_Sqlite.sqlite"
PACK_ROOT = PROJECT_ROOT / "domain-packs" / "chinook" / "v1"
REPORT_PATH = PROJECT_ROOT / "tests" / "reports" / "d4-domain-pack-v1.json"

FIXED_CREATED_AT = "2026-09-26T07:53:00Z"

SOURCE_DATABASE = "source.chinook_database"
SOURCE_SCHEMA = "source.chinook_schema_extract"
SOURCE_VOCAB = "source.d3_contract_vocabulary"

# 与 A 冻结的 EXPECTED_DOMAIN_FILES（tools/evaluation/validate_d4_evaluation_freeze.py）
# 保持同一 role -> path 映射。
FILE_ROLES: list[tuple[str, str, str, bool]] = [
    ("concepts", "concepts.json", "L1", True),
    ("aliases", "aliases.json", "L1", True),
    ("slots", "slots.json", "L0", True),
    ("schema_catalog", "schema-catalog.json", "L1", True),
    ("relations", "relations.json", "L1", True),
    ("value_evidence", "value-evidence.json", "L1", True),
    ("validation_rules", "validation-rules.json", "L0", True),
    ("license_notice", "LICENSE-or-SOURCE.md", "L0", False),
]

METRIC_KINDS = [
    "count",
    "revenue",
    "sales_revenue",
    "invoice_total",
    "unit_price",
    "duration",
    "growth_rate",
]
ENTITY_KINDS = [
    "artist",
    "album",
    "track",
    "genre",
    "media_type",
    "playlist",
    "playlist_track",
    "customer",
    "invoice",
    "invoice_line",
    "employee",
]
GRAIN_KINDS = ["day", "month", "quarter", "year"]
SORT_KINDS = ["asc", "desc"]


def sha256_lf(path: Path) -> str:
    """D4 文本资产 hash：字节级 CRLF/CR -> LF 规范化后 SHA-256。"""
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(content).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def read_database_facts() -> tuple[list[str], int, int]:
    """只读查询：国家枚举与发票年份范围（值证据的授权来源）。"""
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        cursor = connection.cursor()
        countries = [
            row[0]
            for row in cursor.execute(
                "SELECT DISTINCT Country FROM Customer ORDER BY Country"
            )
        ]
        year_min, year_max = cursor.execute(
            "SELECT MIN(CAST(substr(InvoiceDate,1,4) AS INTEGER)),"
            " MAX(CAST(substr(InvoiceDate,1,4) AS INTEGER)) FROM Invoice"
        ).fetchone()
    finally:
        connection.close()
    return countries, int(year_min), int(year_max)


def build_concepts(schema: dict[str, Any]) -> list[dict[str, Any]]:
    items = [
        {
            "concept_id": "concept.music_store",
            "display_name": "音乐商店业务",
            "description": "Chinook 公开样例数据库对应的音乐商店业务域，涵盖曲库、销售与员工三类实体。",
            "parent_concept_ids": [],
            "source_refs": [SOURCE_SCHEMA],
        }
    ]
    for table_name, table in schema["tables"].items():
        suffix = table_name.lower()
        items.append(
            {
                "concept_id": f"concept.{suffix}",
                "display_name": table["description"],
                "description": (
                    f"音乐商店{table['description']}实体，对应 {table_name} 表"
                    f"（约 {table['row_count']} 行）。"
                ),
                "parent_concept_ids": ["concept.music_store"],
                "source_refs": [SOURCE_SCHEMA],
            }
        )
    return items


def build_aliases(schema: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for table_name, table in schema["tables"].items():
        suffix = table_name.lower()
        for index, surface in enumerate(table.get("aliases", [])):
            items.append(
                {
                    "alias_id": f"alias.{suffix}_zh_{index + 1}",
                    "surface_form": surface,
                    "language": "zh-CN",
                    "concept_id": f"concept.{suffix}",
                    "source_ref": SOURCE_SCHEMA,
                    "confidence_class": "curated",
                }
            )
        items.append(
            {
                "alias_id": f"alias.{suffix}_en",
                "surface_form": table_name,
                "language": "en",
                "concept_id": f"concept.{suffix}",
                "source_ref": SOURCE_SCHEMA,
                "confidence_class": "authoritative",
            }
        )
    return items


def build_slots() -> list[dict[str, Any]]:
    """槽位词汇与 D3 冻结契约对齐（prompt_dev 输入侧键名/取值令牌）。

    required_when 为 v1 业务声明式基线；自动发现与阈值调优属 D4-04（A）。
    """
    return [
        {
            "slot_id": "slot.metric",
            "display_name": "指标",
            "description": "问数请求度量的业务指标，如收入、单价、时长或增长率。",
            "value_type": "enum",
            "required_by_default": True,
            "required_when": [],
            "conflict_slot_ids": [],
            "allowed_value_evidence_refs": ["evidence.metric_kinds"],
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "slot_id": "slot.entity_type",
            "display_name": "实体类型",
            "description": "问题主体所属的业务实体种类；跨多表的指标必须明确主体。",
            "value_type": "enum",
            "required_by_default": False,
            "required_when": [
                {
                    "condition_id": "condition.metric_entity_attached",
                    "subject_ref": "slot.metric",
                    "operator": "in",
                    "values": ["count", "unit_price", "duration"],
                }
            ],
            "conflict_slot_ids": [],
            "allowed_value_evidence_refs": ["evidence.entity_kinds"],
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "slot_id": "slot.time_range",
            "display_name": "时间范围",
            "description": "统计覆盖的时间区间；收入与增长率等期间型指标需要明确期间。",
            "value_type": "string",
            "required_by_default": False,
            "required_when": [
                {
                    "condition_id": "condition.metric_period_aggregate",
                    "subject_ref": "slot.metric",
                    "operator": "in",
                    "values": ["revenue", "sales_revenue", "invoice_total", "growth_rate"],
                }
            ],
            "conflict_slot_ids": [],
            "allowed_value_evidence_refs": ["evidence.time_range_years"],
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "slot_id": "slot.country",
            "display_name": "国家",
            "description": "客户或发票归属的国家，取值必须来自数据库实际国家集合。",
            "value_type": "enum",
            "required_by_default": False,
            "required_when": [
                {
                    "condition_id": "condition.entity_geo_attached",
                    "subject_ref": "slot.entity_type",
                    "operator": "in",
                    "values": ["customer", "invoice"],
                }
            ],
            "conflict_slot_ids": [],
            "allowed_value_evidence_refs": ["evidence.country_enum"],
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "slot_id": "slot.limit",
            "display_name": "前N条数",
            "description": "Top-N 类问题返回的条数上限，必须为正整数。",
            "value_type": "integer",
            "required_by_default": False,
            "required_when": [
                {
                    "condition_id": "condition.metric_ranking",
                    "subject_ref": "slot.metric",
                    "operator": "in",
                    "values": ["unit_price", "duration"],
                }
            ],
            "conflict_slot_ids": [],
            "allowed_value_evidence_refs": ["evidence.limit_range"],
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "slot_id": "slot.grain",
            "display_name": "时间粒度",
            "description": "期间对比使用的分组粒度；增长率类指标需要明确对比粒度。",
            "value_type": "enum",
            "required_by_default": False,
            "required_when": [
                {
                    "condition_id": "condition.metric_growth",
                    "subject_ref": "slot.metric",
                    "operator": "equals",
                    "values": ["growth_rate"],
                }
            ],
            "conflict_slot_ids": [],
            "allowed_value_evidence_refs": ["evidence.grain_kinds"],
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "slot_id": "slot.sort",
            "display_name": "排序方向",
            "description": "Top-N 结果的排序方向；指定条数上限时需要排序方向。",
            "value_type": "enum",
            "required_by_default": False,
            "required_when": [
                {
                    "condition_id": "condition.limit_present",
                    "subject_ref": "slot.limit",
                    "operator": "present",
                    "values": [],
                }
            ],
            "conflict_slot_ids": [],
            "allowed_value_evidence_refs": ["evidence.sort_kinds"],
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "slot_id": "slot.comparison_baseline",
            "display_name": "对比基线",
            "description": "增长率类指标对比的基准期间，如上一季度或上一年度。",
            "value_type": "string",
            "required_by_default": False,
            "required_when": [
                {
                    "condition_id": "condition.metric_growth_baseline",
                    "subject_ref": "slot.metric",
                    "operator": "equals",
                    "values": ["growth_rate"],
                }
            ],
            "conflict_slot_ids": [],
            "allowed_value_evidence_refs": [],
            "source_refs": [SOURCE_VOCAB],
        },
    ]


def build_schema_catalog(schema: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "object_id": "schema.chinook",
            "object_type": "data_source",
            "parent_object_id": None,
            "display_name": "Chinook 音乐商店数据库",
            "physical_name": "chinook-v1.4.5",
            "data_type": None,
            "concept_refs": ["concept.music_store"],
            "authorization_level": "read_public",
            "source_refs": [SOURCE_SCHEMA],
        }
    ]
    for table_name, table in schema["tables"].items():
        table_suffix = table_name.lower()
        items.append(
            {
                "object_id": f"schema.chinook.{table_suffix}",
                "object_type": "table",
                "parent_object_id": "schema.chinook",
                "display_name": table["description"],
                "physical_name": table_name,
                "data_type": None,
                "concept_refs": [f"concept.{table_suffix}"],
                "authorization_level": "read_public",
                "source_refs": [SOURCE_SCHEMA],
            }
        )
        for column_name, column in table["columns"].items():
            items.append(
                {
                    "object_id": f"schema.chinook.{table_suffix}.{column_name.lower()}",
                    "object_type": "column",
                    "parent_object_id": f"schema.chinook.{table_suffix}",
                    "display_name": column["description"],
                    "physical_name": column_name,
                    "data_type": column["type"],
                    "concept_refs": [],
                    "authorization_level": "read_public",
                    "source_refs": [SOURCE_SCHEMA],
                }
            )
    return items


def build_relations(schema: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for relationship in schema["relationships"]:
        source_table, source_column = relationship["from"].split(".")
        target_table, target_column = relationship["to"].split(".")
        relation_id = (
            f"relation.fk_{source_table.lower()}_{source_column.lower()}"
        )
        items.append(
            {
                "relation_id": relation_id,
                "relation_type": "foreign_key",
                "source_object_id": (
                    f"schema.chinook.{source_table.lower()}.{source_column.lower()}"
                ),
                "target_object_id": (
                    f"schema.chinook.{target_table.lower()}.{target_column.lower()}"
                ),
                "direction": "directed",
                "cardinality": "many_to_one",
                "source_refs": [SOURCE_SCHEMA],
            }
        )
    return items


def build_value_evidence(countries: list[str], year_min: int, year_max: int) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": "evidence.metric_kinds",
            "slot_id": "slot.metric",
            "evidence_type": "enum",
            "payload": {"values": METRIC_KINDS},
            "data_level": "L0",
            "send_to_model": True,
            "source_ref": SOURCE_VOCAB,
        },
        {
            "evidence_id": "evidence.entity_kinds",
            "slot_id": "slot.entity_type",
            "evidence_type": "enum",
            "payload": {"values": ENTITY_KINDS},
            "data_level": "L1",
            "send_to_model": True,
            "source_ref": SOURCE_SCHEMA,
        },
        {
            "evidence_id": "evidence.time_range_years",
            "slot_id": "slot.time_range",
            "evidence_type": "range",
            "payload": {"minimum": year_min, "maximum": year_max},
            "data_level": "L1",
            "send_to_model": True,
            "source_ref": SOURCE_DATABASE,
        },
        {
            "evidence_id": "evidence.country_enum",
            "slot_id": "slot.country",
            "evidence_type": "enum",
            "payload": {"values": countries},
            "data_level": "L1",
            "send_to_model": True,
            "source_ref": SOURCE_DATABASE,
        },
        {
            "evidence_id": "evidence.limit_range",
            "slot_id": "slot.limit",
            "evidence_type": "range",
            "payload": {"minimum": 1},
            "data_level": "L0",
            "send_to_model": True,
            "source_ref": SOURCE_VOCAB,
        },
        {
            "evidence_id": "evidence.grain_kinds",
            "slot_id": "slot.grain",
            "evidence_type": "enum",
            "payload": {"values": GRAIN_KINDS},
            "data_level": "L0",
            "send_to_model": True,
            "source_ref": SOURCE_VOCAB,
        },
        {
            "evidence_id": "evidence.sort_kinds",
            "slot_id": "slot.sort",
            "evidence_type": "enum",
            "payload": {"values": SORT_KINDS},
            "data_level": "L0",
            "send_to_model": True,
            "source_ref": SOURCE_VOCAB,
        },
    ]


def build_validation_rules() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": "rule.metric_must_be_known",
            "rule_kind": "hard",
            "operator": "in",
            "input_refs": ["slot.metric"],
            "evidence_refs": ["evidence.metric_kinds"],
            "reason_code": "metric_unknown",
            "recommended_action": "clarify",
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "rule_id": "rule.entity_type_must_be_known",
            "rule_kind": "hard",
            "operator": "in",
            "input_refs": ["slot.entity_type"],
            "evidence_refs": ["evidence.entity_kinds"],
            "reason_code": "entity_type_unknown",
            "recommended_action": "clarify",
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "rule_id": "rule.country_must_be_known",
            "rule_kind": "hard",
            "operator": "in",
            "input_refs": ["slot.country"],
            "evidence_refs": ["evidence.country_enum"],
            "reason_code": "country_outside_evidence",
            "recommended_action": "clarify",
            "source_refs": [SOURCE_DATABASE],
        },
        {
            "rule_id": "rule.limit_must_be_positive",
            "rule_kind": "hard",
            "operator": "in",
            "input_refs": ["slot.limit"],
            "evidence_refs": ["evidence.limit_range"],
            "reason_code": "limit_out_of_range",
            "recommended_action": "clarify",
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "rule_id": "rule.time_range_must_be_covered",
            "rule_kind": "hard",
            "operator": "in",
            "input_refs": ["slot.time_range"],
            "evidence_refs": ["evidence.time_range_years"],
            "reason_code": "time_range_outside_data",
            "recommended_action": "clarify",
            "source_refs": [SOURCE_DATABASE],
        },
        {
            "rule_id": "rule.grain_must_be_known",
            "rule_kind": "hard",
            "operator": "in",
            "input_refs": ["slot.grain"],
            "evidence_refs": ["evidence.grain_kinds"],
            "reason_code": "grain_unknown",
            "recommended_action": "clarify",
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "rule_id": "rule.sort_must_be_known",
            "rule_kind": "hard",
            "operator": "in",
            "input_refs": ["slot.sort"],
            "evidence_refs": ["evidence.sort_kinds"],
            "reason_code": "sort_unknown",
            "recommended_action": "clarify",
            "source_refs": [SOURCE_VOCAB],
        },
        {
            "rule_id": "rule.grain_without_time_range",
            "rule_kind": "soft",
            "operator": "conflicts",
            "input_refs": ["slot.grain", "slot.time_range"],
            "evidence_refs": [],
            "reason_code": "grain_without_time_range",
            "recommended_action": "clarify",
            "source_refs": [SOURCE_VOCAB],
        },
    ]


LICENSE_NOTICE = """# Chinook 领域包来源与许可声明

## 来源

- `source.chinook_database`：Chinook Sample Database v1.4.5（MIT），
  https://github.com/lerocha/chinook-database/releases/tag/v1.4.5 。
  仅读取公开样例数据的聚合值集合（国家枚举、发票年份范围），未包含任何客户个人信息。
- `source.chinook_schema_extract`：本仓库冻结的 schema 描述文件
  `data/schema/chinook-v1.4.5.schema.json`（项目内部内容）。
- `source.d3_contract_vocabulary`：D3 冻结契约的槽位词汇
  （`configs/prompts/clarification/v1/system.md` 契约语义；槽位键名与取值令牌
  和 prompt_dev 输入侧 `context.required_slots` / `input.known_slots` 对齐）。

## 数据边界

- 构建过程不读取任何金标 `expected` / `gold_sql` / `result` 字段。
- validation / test split 内容零进入本包。
- 值证据仅含公开样例数据库的聚合值集合，不含客户级个人数据。
- 本包不含隐藏测试题、答案或金标 SQL。

## 许可与数据级别

- 外部事实：MIT（Chinook）。
- 项目内部内容：随仓库分发。
- 数据级别：包级 L1；`slots.json` 与 `validation-rules.json` 为 L0。
"""


def build_manifest(file_hashes: dict[str, str]) -> dict[str, Any]:
    return {
        "domain_pack_id": "chinook_music_store",
        "domain_id": "chinook_music",
        "version": "1.0.0",
        "schema_version": "1.0.0",
        "content_schema_id": "domain-pack-content.v1",
        "language": "zh-CN",
        "created_at": FIXED_CREATED_AT,
        "data_level": "L1",
        "source_refs": [
            {
                "source_id": SOURCE_DATABASE,
                "uri": "https://github.com/lerocha/chinook-database/releases/tag/v1.4.5",
                "license": "MIT",
                "data_level": "L1",
                "authorized": True,
            },
            {
                "source_id": SOURCE_SCHEMA,
                "uri": "repo:data/schema/chinook-v1.4.5.schema.json",
                "license": "MIT",
                "data_level": "L1",
                "authorized": True,
            },
            {
                "source_id": SOURCE_VOCAB,
                "uri": "repo:configs/prompts/clarification/v1/system.md",
                "license": "MIT",
                "data_level": "L0",
                "authorized": True,
            },
        ],
        "license": {
            "spdx_id": "MIT",
            "redistribution_allowed": True,
            "notice_path": "LICENSE-or-SOURCE.md",
        },
        "compatibility": {
            "task_types": ["intent", "clarification", "nl2sql"],
            "database_versions": ["chinook-v1.4.5"],
        },
        "hash_algorithm": "sha256-utf8-lf",
        "files": [
            {
                "role": role,
                "path": path,
                "sha256": file_hashes[role],
                "data_level": level,
                "send_to_model": send,
            }
            for role, path, level, send in FILE_ROLES
        ],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="构建 Chinook 领域语义包 v1")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="输出根目录（默认仓库根）；用于重建确定性测试的临时目录。",
    )
    args = parser.parse_args(argv)
    output_root = args.output_root
    pack_root = PACK_ROOT
    report_path = REPORT_PATH
    if output_root is not None:
        pack_root = output_root / "domain-packs" / "chinook" / "v1"
        report_path = output_root / "tests" / "reports" / "d4-domain-pack-v1.json"

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    countries, year_min, year_max = read_database_facts()

    pack_root.mkdir(parents=True, exist_ok=True)

    artifacts: dict[str, Any] = {
        "concepts": {
            "schema_version": "1.0.0",
            "artifact_type": "concepts",
            "items": build_concepts(schema),
        },
        "aliases": {
            "schema_version": "1.0.0",
            "artifact_type": "aliases",
            "items": build_aliases(schema),
        },
        "slots": {
            "schema_version": "1.0.0",
            "artifact_type": "slots",
            "items": build_slots(),
        },
        "schema_catalog": {
            "schema_version": "1.0.0",
            "artifact_type": "schema_catalog",
            "items": build_schema_catalog(schema),
        },
        "relations": {
            "schema_version": "1.0.0",
            "artifact_type": "relations",
            "items": build_relations(schema),
        },
        "value_evidence": {
            "schema_version": "1.0.0",
            "artifact_type": "value_evidence",
            "items": build_value_evidence(countries, year_min, year_max),
        },
        "validation_rules": {
            "schema_version": "1.0.0",
            "artifact_type": "validation_rules",
            "items": build_validation_rules(),
        },
    }

    role_to_path = {role: path for role, path, _, _ in FILE_ROLES}
    file_hashes: dict[str, str] = {}
    for role, payload in artifacts.items():
        target = pack_root / role_to_path[role]
        write_json(target, payload)
        file_hashes[role] = sha256_lf(target)

    notice_path = pack_root / "LICENSE-or-SOURCE.md"
    write_text(notice_path, LICENSE_NOTICE)
    file_hashes["license_notice"] = sha256_lf(notice_path)

    manifest = build_manifest(file_hashes)
    write_json(pack_root / "manifest.json", manifest)

    report = {
        "status": "pass",
        "report_type": "d4_domain_pack_offline_build",
        "task_id": "D4-03",
        "scope": "chinook_domain_pack_v1_data_authoring_only",
        "generated_by": "tools/rag/build_chinook_domain_pack.py",
        "domain_pack": {
            "domain_pack_id": manifest["domain_pack_id"],
            "domain_id": manifest["domain_id"],
            "version": manifest["version"],
            "path": "domain-packs/chinook/v1",
            "manifest_sha256": sha256_lf(PACK_ROOT / "manifest.json"),
            "item_counts": {
                "concepts": len(artifacts["concepts"]["items"]),
                "aliases": len(artifacts["aliases"]["items"]),
                "slots": len(artifacts["slots"]["items"]),
                "schema_catalog_objects": len(artifacts["schema_catalog"]["items"]),
                "relations": len(artifacts["relations"]["items"]),
                "value_evidence": len(artifacts["value_evidence"]["items"]),
                "validation_rules": len(artifacts["validation_rules"]["items"]),
            },
            "country_count": len(countries),
            "invoice_year_range": [year_min, year_max],
        },
        "sources_read": [
            "data/schema/chinook-v1.4.5.schema.json",
            "data/raw/chinook-v1.4.5/Chinook_Sqlite.sqlite (read-only)",
            "D3 frozen contract slot vocabulary (prompt_dev input-side keys)",
        ],
        "input_isolation": {
            "expected_fields_read": False,
            "gold_sql_read": False,
            "validation_or_test_content_in_pack": False,
        },
        "determinism": {
            "fixed_created_at": FIXED_CREATED_AT,
            "rebuild_reproducible": True,
        },
        "model_boundary": {"llm_calls": 0},
        "builder_sqlite_reads": 2,
        "sql_executions": 0,
        "network_calls": 0,
        "model_api_calls": 0,
        "estimated_cost_cny": 0.0,
        "not_implemented": [
            "DomainPackRegistry (A, src/backend/agent/)",
            "production CandidateRetriever (A+C, D4-03 A-side)",
            "required_slots auto-discovery (D4-04, A)",
            "empirical calibration (D4-05, A)",
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(report_path, report)

    print(f"domain pack written: {pack_root}")
    for role, path, _, _ in FILE_ROLES:
        print(f"  {role:<17} {path:<22} sha256={file_hashes[role][:16]}...")
    print(f"report written: {report_path}")


if __name__ == "__main__":
    main()
