#!/usr/bin/env python3
"""Offline, two-scale SQL component benchmark; no provider or network access."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import platform
import shutil
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from backend.nl2sql.readonly_executor import DEFAULT_DB_PATH, ReadonlyExecutor  # noqa: E402
from backend.nl2sql.sql_safety_gate import SafetyDecision, validate_candidate  # noqa: E402

REPORT = ROOT / "tests" / "reports" / "d5-08-b-two-scale-sql-v1.json"
SCALES = (1_000, 10_000)
QUERY = "SELECT ArtistId, Name FROM Artist ORDER BY Name"


def percentile(values: list[float], fraction: float) -> float:
    if not values or not 0 <= fraction <= 1:
        raise ValueError("invalid_percentile_input")
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def expand_artist(db_path: Path, target_rows: int) -> int:
    """Only mutate a disposable database copy; fixture names never enter reports."""
    with closing(sqlite3.connect(db_path)) as conn:
        original = conn.execute("SELECT COUNT(*) FROM Artist").fetchone()[0]
        if target_rows < original:
            raise ValueError("target_rows_below_source")
        next_id = conn.execute("SELECT MAX(ArtistId) FROM Artist").fetchone()[0] + 1
        conn.executemany(
            "INSERT INTO Artist (ArtistId, Name) VALUES (?, ?)",
            ((next_id + index, f"Synthetic Artist {index:05d}")
             for index in range(target_rows - original)),
        )
        actual = conn.execute("SELECT COUNT(*) FROM Artist").fetchone()[0]
        conn.commit()
    if actual != target_rows:
        raise RuntimeError("fixture_row_count_mismatch")
    return actual


def check_guards(executor: ReadonlyExecutor) -> dict[str, bool]:
    over_limit = not validate_candidate(
        "SELECT ArtistId FROM Artist LIMIT 500", ["Artist"], [], max_rows=100
    ).approved
    write_rejected = not validate_candidate(
        "DELETE FROM Artist", ["Artist"], [], max_rows=100
    ).approved
    forged = SafetyDecision(
        approved=True, execution_sql="UPDATE Artist SET Name='x'",
        tables=("Artist",), row_limit=100,
    )
    try:
        executor.execute(forged)
    except ValueError:
        forged_rejected = True
    else:
        forged_rejected = False
    result = {
        "excessive_limit_rejected": over_limit,
        "write_rejected": write_rejected,
        "forged_decision_rejected": forged_rejected,
    }
    if not all(result.values()):
        raise RuntimeError("sql_guard_failure")
    return result


def benchmark_scale(db_path: Path, *, rows: int, samples: int, warmup: int) -> dict[str, object]:
    if samples < 2 or warmup < 0:
        raise ValueError("invalid_sample_configuration")
    executor = ReadonlyExecutor(db_path=db_path, max_rows=100)
    guards = check_guards(executor)
    latencies: list[float] = []
    peak_bytes = 0
    try:
        for index in range(warmup + samples):
            if index == warmup:
                tracemalloc.start()
            started = time.perf_counter()
            decision = validate_candidate(QUERY, ["Artist"], [], max_rows=100)
            if (not decision.approved or decision.row_limit != 100
                    or not (decision.execution_sql or "").endswith("LIMIT 100")):
                raise RuntimeError("sql_safety_gate_rejected_benchmark_query")
            result = executor.execute(decision)
            elapsed = (time.perf_counter() - started) * 1000
            if result.row_count != 100:
                raise RuntimeError("sql_row_limit_failure")
            if index >= warmup:
                latencies.append(elapsed)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
    return {
        "artist_rows_verified": rows,
        "warmup_runs": warmup,
        "measured_runs": samples,
        "p50_ms": round(percentile(latencies, 0.5), 3),
        "p95_ms": round(percentile(latencies, 0.95), 3),
        "max_ms": round(max(latencies), 3),
        "python_tracemalloc_peak_bytes": peak_bytes,
        "row_limit": executor.max_rows,
        "rows_returned_each_run": 100,
        "limit_injected_each_run": True,
        "guards": guards,
        "errors": 0,
    }


def build_report(*, samples: int = 50, warmup: int = 5) -> dict[str, object]:
    if samples < 2 or warmup < 0:
        raise ValueError("invalid_sample_configuration")
    source_hash = digest(DEFAULT_DB_PATH)
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="d5-08-b-sql-", dir=scratch) as temporary:
        scales = []
        for target in SCALES:
            db_path = Path(temporary) / f"scale-{target}.sqlite"
            shutil.copyfile(DEFAULT_DB_PATH, db_path)
            actual = expand_artist(db_path, target)
            scales.append(benchmark_scale(db_path, rows=actual, samples=samples, warmup=warmup))
    if digest(DEFAULT_DB_PATH) != source_hash:
        raise RuntimeError("source_database_modified")
    executor = ReadonlyExecutor()
    return {
        "task_id": "D5-08-B",
        "evidence_kind": "offline_two_scale_sql_component_performance",
        "status": "pass",
        "platform": platform.system().lower(),
        "source_sha256": source_hash,
        "scope": "local_in_process_safety_gate_and_readonly_executor",
        "concurrency": 1,
        "network_transport": "none",
        "scales": scales,
        "limits": {
            "max_rows": executor.max_rows,
            "max_vm_steps": executor.max_vm_steps,
            "max_elapsed_ms": executor.max_elapsed_ms,
        },
        "boundaries": {
            "memory_metric": "python_tracemalloc_peak_not_process_rss",
            "real_model_api_calls": 0,
            "real_key_read": False,
            "business_accuracy_measured": False,
            "end_to_end_latency_measured": False,
            "final_gate": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=5)
    args = parser.parse_args()
    report = build_report(samples=args.samples, warmup=args.warmup)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if any(marker in serialized.casefold() for marker in ("sk-", "ictagent123", "select ", "insert ")):
        raise RuntimeError("sensitive_report_content")
    REPORT.write_text(serialized, encoding="utf-8")
    print(json.dumps({"status": report["status"], "scales": report["scales"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
