"""SQL 安全网关：候选 SQL 执行前的单表/只读/白名单/参数化/限行校验。

D4-02 核心。候选 SQL 由模型生成，执行前必须通过本网关，绝不直接执行。

校验分三层：
1. 文本结构校验（快速失败、明确报错）：单语句、无注释、无子查询、无 JOIN、
   无写操作/DDL/危险关键字、必须以 SELECT 开头。
2. 参数化校验：SQL 中 ``?`` 占位符数量必须等于 ``params`` 长度。
3. authorizer C 层校验（SQLite 权威判定）：在内存连接上按声明表建立结构骨架后编译执行，
   authorizer 拦截写操作、ATTACH/DETACH、PRAGMA、非白名单函数，并记录被读取的表集合，
   要求恰好等于声明的单表。

限行：无 LIMIT 时注入 ``LIMIT max_rows``，有 LIMIT 时校验不超过上限。

仅离线：网络调用 0，模型 API 调用 0，不读真实密钥，不读评测集答案字段。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

# SQLite authorizer action codes（见 sqlite3.h SQLITE_*）。
_WRITE_AND_DDL = {
    1, 2, 3, 4, 5, 6, 7, 8,          # CREATE_*
    9,                                # DELETE
    10, 11, 12, 13, 14, 15, 16, 17,   # DROP_*
    18,                               # INSERT
    23,                               # UPDATE
    26, 27, 28, 29, 30,               # ALTER/REINDEX/ANALYZE/CREATE_VTABLE/DROP_VTABLE
    32,                               # SAVEPOINT
}
_SQLITE_ATTACH = 24
_SQLITE_DETACH = 25
_SQLITE_PRAGMA = 19
_SQLITE_READ = 20
_SQLITE_FUNCTION = 31

_SQLITE_OK = 0
_SQLITE_DENY = 1

# 白名单函数（大写比较）。与 nl2sql.v1 prompt 允许集合一致，另加 LOWER（大小写不敏感 LIKE）。
ALLOWED_FUNCTIONS = {"COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND", "LOWER"}

# 文本结构拒绝规则（结构校验，非关键词黑名单掩盖失败）。
_SEMICOLON = re.compile(r";")
_COMMENT = re.compile(r"--|/\*|\*/")
_SUBQUERY = re.compile(r"\(\s*SELECT\b", re.IGNORECASE)
_JOIN = re.compile(r"\bJOIN\b", re.IGNORECASE)
_UNION = re.compile(r"\bUNION\b", re.IGNORECASE)
_CTE = re.compile(r"\bWITH\b", re.IGNORECASE)
_WRITE_KEYWORD = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)
_DANGEROUS_KEYWORD = re.compile(
    r"\b(ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE)\b", re.IGNORECASE
)
_SELECT_START = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_LIMIT_KEYWORD = re.compile(r"\bLIMIT\b", re.IGNORECASE)
_LIMIT_AT_END = re.compile(
    r"\bLIMIT\s+(\d+)(?:\s+OFFSET\s+\d+)?\s*$", re.IGNORECASE
)
_QUOTED_SEGMENT = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[(?:\]\]|[^\]])*\]"
)

# NVARCHAR/NUMERIC/DATETIME -> SQLite 亲和类型（仅用于建结构骨架）。
_TYPE_MAP = {
    "INTEGER": "INTEGER",
    "NVARCHAR": "TEXT",
    "NUMERIC": "REAL",
    "DATETIME": "TEXT",
}


@dataclass(frozen=True)
class SafetyDecision:
    approved: bool
    reason: str | None = None
    execution_sql: str | None = None
    tables: tuple[str, ...] = ()
    row_limit: int | None = None


def _column_type(sql_type: str) -> str:
    base = sql_type.split("(")[0].upper()
    return _TYPE_MAP.get(base, "TEXT")


def _mask_quoted_segments(sql: str) -> str:
    """Hide literals/quoted identifiers before scanning SQL structure.

    This prevents words such as ``LIMIT`` or comment markers inside a string
    literal from being mistaken for SQL syntax. SQLite remains the final
    parser and authorizer for the unmodified statement.
    """

    return _QUOTED_SEGMENT.sub(lambda match: " " * len(match.group(0)), sql)


def _build_table_skeleton(conn: sqlite3.Connection, table_name: str, catalog: dict) -> None:
    """在内存连接上建立单表结构骨架，使 authorizer 的 READ 能正确触发。"""
    columns = catalog["columns"]
    col_defs = ", ".join(f'"{col}" {_column_type(meta["type"])}' for col, meta in columns.items())
    conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')


class _Probe:
    """authorizer 回调状态容器：记录被读取的表。"""

    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.select_count = 0


def _make_authorizer(probe: _Probe):
    def authorizer(action, arg1, arg2, db_name, trigger_name):
        del db_name, trigger_name
        if action == sqlite3.SQLITE_SELECT:
            probe.select_count += 1
        if action in _WRITE_AND_DDL:
            return _SQLITE_DENY
        if action in (_SQLITE_ATTACH, _SQLITE_DETACH):
            return _SQLITE_DENY
        if action == _SQLITE_PRAGMA:
            return _SQLITE_DENY
        if action == _SQLITE_FUNCTION:
            if str(arg2).upper() not in ALLOWED_FUNCTIONS:
                return _SQLITE_DENY
        if action == _SQLITE_READ:
            probe.tables.add(str(arg1))
        return _SQLITE_OK

    return authorizer


def _text_structure_rejection(sql: str) -> str | None:
    masked = _mask_quoted_segments(sql)
    if _SEMICOLON.search(masked):
        return "多语句"
    if _COMMENT.search(masked):
        return "含注释"
    if not _SELECT_START.match(masked):
        return "非 SELECT 语句"
    if _SUBQUERY.search(masked):
        return "含子查询"
    if _JOIN.search(masked):
        return "含 JOIN"
    if _UNION.search(masked):
        return "含 UNION"
    if _CTE.search(masked):
        return "含 WITH（CTE）"
    if _WRITE_KEYWORD.search(masked):
        return "含写操作或 DDL"
    if _DANGEROUS_KEYWORD.search(masked):
        return "含危险关键字（ATTACH/DETACH/PRAGMA 等）"
    return None


def _authorizer_rejection(sql: str, params: tuple, table_name: str, catalog: dict) -> str | None:
    conn = sqlite3.connect(":memory:")
    _build_table_skeleton(conn, table_name, catalog)
    probe = _Probe()
    conn.set_authorizer(_make_authorizer(probe))
    try:
        # Compile only: never run the candidate while validating it. The
        # skeleton deliberately has no indexes, so every persistent read
        # cursor represents a table reference, including comma self-joins.
        program = conn.execute("EXPLAIN " + sql, params).fetchall()
    except sqlite3.DatabaseError as exc:
        message = str(exc)
        if "not authorized" in message:
            return "authorizer 拒绝（写操作/危险函数/ATTACH/PRAGMA 等）"
        if "no such table" in message:
            return "SQL 访问了未声明或不存在的表"
        if "no such column" in message:
            return "SQL 访问了未声明或不存在的列"
        return f"SQL 编译失败: {message}"
    finally:
        conn.close()

    if probe.select_count != 1:
        return "只能包含一个 SELECT 查询块"
    if sum(instruction[1] == "OpenRead" for instruction in program) != 1:
        return "只能包含一个单表读取游标（禁止隐式自连接）"
    if probe.tables != {table_name}:
        return f"访问表集合 {sorted(probe.tables)} 不等于声明的单表 [{table_name}]"
    return None


def _apply_row_limit(sql: str, max_rows: int) -> tuple[str | None, str | None]:
    masked = _mask_quoted_segments(sql)
    if _LIMIT_KEYWORD.search(masked):
        limit_match = _LIMIT_AT_END.search(masked)
        if limit_match is None:
            return None, "LIMIT 必须是语句末尾的非负整数字面量，可选 OFFSET 整数"
        if _LIMIT_KEYWORD.search(masked[: limit_match.start()]):
            return None, "只能包含一个 LIMIT 子句"
        existing = int(limit_match.group(1))
        if existing > max_rows:
            return None, f"LIMIT {existing} 超过上限 {max_rows}"
        return sql, None
    return f"{sql.rstrip()} LIMIT {max_rows}", None


def validate_candidate(
    sql: str,
    tables: list[str],
    params: list,
    *,
    max_rows: int = 100,
) -> SafetyDecision:
    """校验候选 SQL；通过则返回带限行的 execution_sql。

    ``catalog`` 为单表目录（来自 ``schema_catalog.single_table_catalog``），
    缺省时由调用方从 Chinook schema 提供；不读取评测集。
    """
    if not isinstance(sql, str) or not sql.strip():
        return SafetyDecision(False, "SQL 为空")
    if not isinstance(tables, list) or len(tables) != 1:
        return SafetyDecision(False, f"必须且只能声明一个表，实际 {tables!r}")
    table_name = tables[0]
    if not isinstance(table_name, str) or not table_name:
        return SafetyDecision(False, "声明表名无效")
    if not isinstance(params, list):
        return SafetyDecision(False, "params 必须是列表")
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
        return SafetyDecision(False, "max_rows 必须是正整数")

    rejection = _text_structure_rejection(sql)
    if rejection:
        return SafetyDecision(False, rejection)

    placeholder_count = _mask_quoted_segments(sql).count("?")
    if placeholder_count != len(params):
        return SafetyDecision(
            False, f"参数占位符数量 {placeholder_count} 与 params 长度 {len(params)} 不一致"
        )

    from .schema_catalog import single_table_catalog

    try:
        catalog = single_table_catalog(table_name)
    except (KeyError, TypeError, ValueError):
        return SafetyDecision(False, "声明了未知表")

    rejection = _authorizer_rejection(sql, tuple(params), table_name, catalog)
    if rejection:
        return SafetyDecision(False, rejection)

    execution_sql, limit_rejection = _apply_row_limit(sql, max_rows)
    if limit_rejection:
        return SafetyDecision(False, limit_rejection)

    return SafetyDecision(
        approved=True,
        execution_sql=execution_sql,
        tables=(table_name,),
        row_limit=max_rows,
    )
