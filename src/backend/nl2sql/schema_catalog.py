"""单表 Schema 目录：从 Chinook 冻结 schema 提取表/列/类型，供 NL2SQL prompt 注入。

D4-02 的核心输入源：让模型只看到单个表的列与类型，不把整库 11 表全量注入。
仅读取冻结的 ``data/schema/chinook-v1.4.5.schema.json``，不读任何评测集或答案字段。
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = PROJECT_ROOT / "data" / "schema" / "chinook-v1.4.5.schema.json"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def list_tables(schema: dict | None = None) -> list[str]:
    schema = schema or load_schema()
    return list(schema["tables"].keys())


def single_table_catalog(table_name: str, schema: dict | None = None) -> dict:
    """返回单表目录：{table, description, columns: {col: {type, primary_key, description}}}。"""
    schema = schema or load_schema()
    tables = schema["tables"]
    if table_name not in tables:
        raise KeyError(f"未知表: {table_name}")
    return tables[table_name]


def render_single_table_for_prompt(table_name: str, schema: dict | None = None) -> str:
    """把单表目录渲染为 prompt 注入文本（表名 + 每列「列名 类型 描述」）。"""
    catalog = single_table_catalog(table_name, schema)
    lines = [f"表 {table_name}（{catalog['description']}）:"]
    for column, meta in catalog["columns"].items():
        flags = " 主键" if meta.get("primary_key") else ""
        lines.append(f"  - {column} {meta['type']}{flags}（{meta['description']}）")
    return "\n".join(lines)
