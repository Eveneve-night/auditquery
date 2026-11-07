"""SQLite 只读执行器：在只读连接上执行通过安全网关的 SQL。

三重只读防护：
1. ``file:...?mode=ro`` 只读 URI（文件系统层拒绝写）。
2. ``PRAGMA query_only = ON``（连接层拒绝写）。
3. authorizer 拦截写操作/ATTACH/PRAGMA（与安全网关一致）。

资源限制：progress handler 限制虚拟机指令数（防恶意长查询/递归），
fetchmany 限行并标记截断。仅离线，不读密钥。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .sql_safety_gate import (
    ALLOWED_FUNCTIONS,
    SafetyDecision,
    _SQLITE_DENY,
    _SQLITE_OK,
    _WRITE_AND_DDL,
    validate_candidate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "raw" / "chinook-v1.4.5" / "Chinook_Sqlite.sqlite"


@dataclass(frozen=True)
class ExecutionResult:
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    row_count: int
    truncated: bool
    elapsed_ms: int


def _readonly_authorizer(action, arg1, arg2, db_name, trigger_name):
    del arg1, db_name, trigger_name
    if action in _WRITE_AND_DDL:
        return _SQLITE_DENY
    if action == 24 or action == 25:  # ATTACH / DETACH
        return _SQLITE_DENY
    if action == 19:  # PRAGMA
        return _SQLITE_DENY
    if action == 31 and str(arg2).upper() not in ALLOWED_FUNCTIONS:
        return _SQLITE_DENY
    return _SQLITE_OK


class ReadonlyExecutor:
    """对 Chinook 只读库执行已通过安全网关的 SQL。"""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        *,
        max_rows: int = 100,
        max_vm_steps: int = 2_000_000,
        max_elapsed_ms: int = 2_000,
    ) -> None:
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        if not isinstance(max_vm_steps, int) or isinstance(max_vm_steps, bool) or max_vm_steps < 1:
            raise ValueError("max_vm_steps must be a positive integer")
        if not isinstance(max_elapsed_ms, int) or isinstance(max_elapsed_ms, bool) or max_elapsed_ms < 0:
            raise ValueError("max_elapsed_ms must be a non-negative integer")
        self.db_path = Path(db_path)
        self.max_rows = max_rows
        self.max_vm_steps = max_vm_steps
        self.max_elapsed_ms = max_elapsed_ms

    def execute(
        self, decision: SafetyDecision, params: list | tuple = ()
    ) -> ExecutionResult:
        """Execute only a decision produced by the safety gate.

        The statement is revalidated immediately before opening the real
        database. This makes the gate an enforced boundary rather than a
        caller convention and rejects forged or stale decisions fail-closed.
        """

        if not isinstance(decision, SafetyDecision):
            raise TypeError("executor requires an approved SafetyDecision")
        if not decision.approved or not decision.execution_sql or len(decision.tables) != 1:
            raise ValueError("executor requires an approved SafetyDecision")
        if not isinstance(params, (list, tuple)):
            raise TypeError("params must be a list or tuple")
        revalidated = validate_candidate(
            decision.execution_sql,
            list(decision.tables),
            list(params),
            max_rows=decision.row_limit or self.max_rows,
        )
        if not revalidated.approved or revalidated.execution_sql != decision.execution_sql:
            raise ValueError("safety decision failed execution-time revalidation")
        sql = decision.execution_sql
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        started = time.monotonic()
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.set_authorizer(_readonly_authorizer)

            progress_interval = min(1_000, self.max_vm_steps)
            observed_steps = 0
            deadline = started + self.max_elapsed_ms / 1000.0

            def _progress() -> int:
                nonlocal observed_steps
                observed_steps += progress_interval
                if observed_steps >= self.max_vm_steps:
                    return 1
                return int(time.monotonic() >= deadline)

            conn.set_progress_handler(_progress, progress_interval)

            cursor = conn.execute(sql, tuple(params))
            columns = tuple(desc[0] for desc in cursor.description) if cursor.description else ()
            rows = cursor.fetchmany(self.max_rows + 1)
            truncated = len(rows) > self.max_rows
            if truncated:
                rows = rows[: self.max_rows]
        finally:
            conn.close()

        elapsed_ms = int((time.monotonic() - started) * 1000)
        return ExecutionResult(
            columns=columns,
            rows=tuple(rows),
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )
