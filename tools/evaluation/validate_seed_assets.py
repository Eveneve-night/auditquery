"""Offline validation for the seed model-evaluation assets.

The validator uses only explicit project paths, never reads API credentials,
and performs no network or model API calls.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "tests" / "goldens" / "model-eval" / "v1" / "manifest.json"
EVIDENCE_PATTERN = re.compile(r"^\[([A-Z]+-[0-9]{3})\]\s+", re.MULTILINE)
REQUIRED_CASE_FIELDS = {"id", "task_type", "split", "input", "context", "expected", "tags"}


class ValidationFailure(Exception):
    pass


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValidationFailure(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return records


def resolve_from(base_file: Path, relative: str) -> Path:
    return (base_file.parent / relative).resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def values_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-9)
    return actual == expected


def validate_cases(manifest_path: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    all_cases: list[dict[str, Any]] = []
    task_counts: dict[str, int] = {}
    for task_name, info in manifest["case_files"].items():
        path = resolve_from(manifest_path, info["path"])
        if not path.is_file():
            raise ValidationFailure(f"Missing case file: {path}")
        cases = read_jsonl(path)
        if len(cases) != info["count"]:
            raise ValidationFailure(
                f"{task_name}: declared {info['count']} cases, found {len(cases)}"
            )
        for case in cases:
            missing = REQUIRED_CASE_FIELDS - case.keys()
            if missing:
                raise ValidationFailure(f"{case.get('id', path.name)} missing fields: {sorted(missing)}")
            if case["task_type"] != task_name:
                raise ValidationFailure(
                    f"{case['id']}: task_type={case['task_type']} does not match {task_name}"
                )
            if case["split"] not in {"prompt_dev", "validation", "test"}:
                raise ValidationFailure(f"{case['id']}: invalid split {case['split']}")
        all_cases.extend(cases)
        task_counts[task_name] = len(cases)

    ids = [case["id"] for case in all_cases]
    duplicates = [case_id for case_id, count in Counter(ids).items() if count > 1]
    if duplicates:
        raise ValidationFailure(f"Duplicate case IDs: {duplicates}")
    actual_splits = Counter(case["split"] for case in all_cases)
    declared_splits = manifest["split_counts"]
    for split in ("prompt_dev", "validation", "test"):
        if actual_splits[split] != declared_splits[split]:
            raise ValidationFailure(
                f"Split {split}: declared {declared_splits[split]}, found {actual_splits[split]}"
            )
    return all_cases, task_counts


def validate_database(
    manifest_path: Path,
    manifest: dict[str, Any],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    database = manifest["database"]
    database_path = resolve_from(manifest_path, database["path"])
    if not database_path.is_file():
        raise ValidationFailure(f"Missing database: {database_path}")
    actual_hash = sha256(database_path)
    if actual_hash != database["sha256"].upper():
        raise ValidationFailure(f"Database hash mismatch: {actual_hash}")

    schema_path = PROJECT_ROOT / "data" / "schema" / "chinook-v1.4.5.schema.json"
    schema = read_json(schema_path)
    connection = sqlite3.connect(database_path)
    sql_cases_checked = 0
    try:
        actual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        if actual_tables != set(schema["tables"]):
            raise ValidationFailure(
                f"Schema table mismatch: database={sorted(actual_tables)}, file={sorted(schema['tables'])}"
            )
        for table_name, table in schema["tables"].items():
            actual_columns = {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')
            }
            if actual_columns != set(table["columns"]):
                raise ValidationFailure(f"{table_name}: column mismatch")
            row_count = connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
            if row_count != table["row_count"]:
                raise ValidationFailure(
                    f"{table_name}: declared {table['row_count']} rows, found {row_count}"
                )

        for case in cases:
            if case["task_type"] != "nl2sql":
                continue
            expected = case["expected"]
            if expected["outcome"] != "sql":
                if "gold_sql" in expected:
                    raise ValidationFailure(f"{case['id']}: rejected case must not contain gold_sql")
                continue
            sql = expected["gold_sql"].strip()
            if not sql.upper().startswith("SELECT ") or ";" in sql:
                raise ValidationFailure(f"{case['id']}: gold SQL must be one SELECT without semicolon")
            cursor = connection.execute(sql, expected.get("params", []))
            columns = [description[0] for description in cursor.description]
            rows = [list(row) for row in cursor.fetchall()]
            expected_result = expected["result"]
            if columns != expected_result["columns"]:
                raise ValidationFailure(f"{case['id']}: result columns mismatch")
            if len(rows) != len(expected_result["rows"]):
                raise ValidationFailure(f"{case['id']}: result row count mismatch")
            for row_index, (actual_row, expected_row) in enumerate(
                zip(rows, expected_result["rows"], strict=True)
            ):
                if len(actual_row) != len(expected_row) or not all(
                    values_equal(actual, expected_value)
                    for actual, expected_value in zip(actual_row, expected_row, strict=True)
                ):
                    raise ValidationFailure(
                        f"{case['id']}: result mismatch at row {row_index}: {actual_row!r}"
                    )
            sql_cases_checked += 1
    finally:
        connection.close()
    return {
        "path": str(database_path.relative_to(PROJECT_ROOT)),
        "sha256": actual_hash,
        "tables_checked": len(schema["tables"]),
        "sql_cases_executed": sql_cases_checked,
    }


def validate_evidence(
    manifest_path: Path,
    manifest: dict[str, Any],
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    document_evidence: dict[str, set[str]] = {}
    all_evidence: set[str] = set()
    for relative in manifest["knowledge_fixtures"]:
        path = resolve_from(manifest_path, relative)
        if not path.is_file():
            raise ValidationFailure(f"Missing knowledge fixture: {path}")
        evidence_ids = set(EVIDENCE_PATTERN.findall(path.read_text(encoding="utf-8")))
        if not evidence_ids:
            raise ValidationFailure(f"No evidence IDs in {path}")
        overlap = all_evidence & evidence_ids
        if overlap:
            raise ValidationFailure(f"Duplicate evidence IDs: {sorted(overlap)}")
        all_evidence |= evidence_ids
        document_evidence[path.stem] = evidence_ids

    rag_cases_checked = 0
    for case in cases:
        if case["task_type"] != "grounded_answer":
            continue
        document_id = case["context"]["document_id"]
        if document_id not in document_evidence:
            raise ValidationFailure(f"{case['id']}: unknown document_id {document_id}")
        provided = set(case["context"]["evidence_ids"])
        supporting = set(case["expected"]["supporting_evidence_ids"])
        if not provided <= document_evidence[document_id]:
            raise ValidationFailure(f"{case['id']}: context references unknown evidence")
        if not supporting <= provided:
            raise ValidationFailure(f"{case['id']}: supporting evidence was not provided")
        if not case["expected"]["required_claims"]:
            raise ValidationFailure(f"{case['id']}: required_claims cannot be empty")
        rag_cases_checked += 1
    return {
        "documents_checked": len(document_evidence),
        "evidence_ids_checked": len(all_evidence),
        "rag_cases_checked": rag_cases_checked,
    }


def validate_loop_assets() -> dict[str, Any]:
    policy_path = PROJECT_ROOT / "configs" / "policies" / "agent-loop-limits.v1.json"
    attacks_path = PROJECT_ROOT / "tests" / "goldens" / "loop-guard" / "v1" / "loop-attacks.jsonl"
    policy = read_json(policy_path)
    attacks = read_jsonl(attacks_path)
    if len(attacks) != 12:
        raise ValidationFailure(f"Expected 12 loop attacks, found {len(attacks)}")
    if len({case["id"] for case in attacks}) != len(attacks):
        raise ValidationFailure("Duplicate loop attack IDs")
    for key, value in policy["limits"].items():
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValidationFailure(f"Loop policy limit {key} must be positive")
    return {
        "policy_id": policy["policy_id"],
        "policy_version": policy["version"],
        "attack_cases_checked": len(attacks),
    }


def validate(manifest_path: Path) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    cases, task_counts = validate_cases(manifest_path, manifest)
    return {
        "status": "pass",
        "dataset_id": manifest["dataset_id"],
        "dataset_version": manifest["version"],
        "total_cases": len(cases),
        "task_counts": task_counts,
        "split_counts": dict(Counter(case["split"] for case in cases)),
        "database": validate_database(manifest_path, manifest, cases),
        "evidence": validate_evidence(manifest_path, manifest, cases),
        "loop_guard": validate_loop_assets(),
        "network_calls": 0,
        "model_api_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = validate(args.manifest.resolve())
    except (OSError, KeyError, TypeError, ValidationFailure, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
