"""Summarize a completed verbose offline unittest run; never infer missing tests.

The log must match discovery on an unchanged tested revision. This is an
author-side validation receipt, not CI attestation or a product release Gate.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import unittest

import cryptography

ROOT = Path(__file__).resolve().parents[2]


def git(*args: str, root: Path | None = None) -> str:
    root = ROOT if root is None else root
    return subprocess.check_output(
        ["git", "-c", "core.quotePath=false", "-C", str(root), *args], text=True, encoding="utf-8"
    ).strip()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test.id()


def inventory():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests/unit"), pattern="test_*.py")
    if loader.errors:
        raise ValueError("test_discovery_import_error")
    return sorted(flatten(suite))


def build_receipt(log_path: Path, tested_commit: str, components=(), probes_dir: Path | None = None):
    commit = git("rev-parse", tested_commit)
    # Require every tracked input in this branch to remain unchanged since
    # the tested commit, except prose and this receipt producer/its report.
    allowed = {
        "tools/evaluation/summarize_batch1_validation.py",
        "tests/reports/batch1-a-offline-validation-v1.json",
        "tests/reports/batch1-joint-offline-validation-v1.json",
    }
    changed = git("diff", "--name-only", commit).splitlines()
    if any(not p.endswith(".md") and p not in allowed for p in changed):
        raise ValueError("tested_inputs_changed")
    text = log_path.read_text(encoding="utf-8-sig")
    ids = inventory()
    observed = re.findall(r"^test_\S+ \(([^)]+)\)", text, re.MULTILINE)
    counts = re.findall(r"^Ran (\d+) tests? in ([0-9.]+)s\s*$", text, re.MULTILINE)
    if len(counts) != 1 or not re.search(r"\nOK\s*$", text):
        raise ValueError("incomplete_or_unsuccessful_run")
    count = int(counts[0][0])
    if len(observed) != count or len(set(observed)) != count or sorted(observed) != ids:
        raise ValueError("test_inventory_mismatch")
    if re.search(r"\.\.\. (?:skipped|FAIL|ERROR)", text):
        raise ValueError("nonpassing_test_in_log")
    inputs = {}
    for line in git("ls-tree", "-r", commit).splitlines():
        metadata, path = line.split("\t", 1)
        if path.startswith(("src/", "configs/", "domain-packs/", "tools/", "tests/unit/")):
            inputs[path] = metadata.split()[2]
    report = {
        "receipt_version": "1.0.0",
        "scope": "author_offline_validation_not_release_gate",
        "tested_commit": commit,
        "tested_tree": git("rev-parse", commit + "^{tree}"),
        "tracked_input_git_blobs_sha256": digest(inputs),
        "runtime": {"python": platform.python_version(), "cryptography": cryptography.__version__,
                    "platform": sys.platform, "local_loopback_allowed": True},
        "command": "python -m unittest discover -s tests/unit -p test_*.py -v",
        "unit_tests": {"discovered": len(ids), "run": count, "passed": count,
                       "failed": 0, "errors": 0, "skipped": [], "import_errors": [],
                       "per_module": dict(sorted(Counter(i.split('.')[0] for i in ids).items())),
                       "test_ids": ids, "test_ids_sha256": digest(ids)},
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
        "not_proven": ["public_adapter_integration", "new_domain_recovery", "business_accuracy",
                       "generalization", "migration_cost", "innovation", "product_release"],
        "boundaries": {"model_api_calls": 0, "real_key_reads": 0,
                       "formal_validation_content_reads": 0, "hidden_test_reads": 0, "cost_cny": 0},
    }
    if components:
        entries = {}
        for component in components:
            role, revision = component.split("=", 1)
            if role in entries or role not in {"main", "A", "B", "C"}:
                raise ValueError("invalid_component_role")
            revision = git("rev-parse", revision)
            subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", revision, commit], check=True)
            entries[role] = {"commit": revision, "tree": git("rev-parse", revision + "^{tree}")}
        report["composition"] = entries
        report["combined_ls_tree_sha256"] = hashlib.sha256(subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-tree", "-r", "--full-tree", commit]
        )).hexdigest()
        report["not_proven"] = ["cross_pack_real_result_display", "new_domain_database_execution",
                                "business_accuracy", "generalization", "migration_cost", "innovation", "product_release"]
    if probes_dir is not None:
        evidence = {}
        for name in ("adapter", "display", "recovery"):
            raw = (probes_dir / (name + ".json")).read_bytes().replace(b"\r\n", b"\n")
            value = json.loads(raw)
            if any(value[k] != 0 for k in ("model_calls", "key_reads", "cost_cny")):
                raise ValueError("probe_boundary_violation")
            rows = value["rows"] if name != "recovery" else value["checks"]
            if not rows:
                raise ValueError("empty_probe")
            if name == "adapter":
                if (value["status"] != "public_path_checked" or
                    value["public_adapter_status"] != "executed" or
                    len(value["results"]) != len(rows) or
                    not all(r["request_valid"] and r["B_output_valid"] for r in rows)):
                    raise ValueError("adapter_probe_incomplete")
                rejected = value["unsafe_request_attacks_rejected"]
                if set(rejected) != {"expected", "gold", "label", "required_slots", "catalog_escape"}:
                    raise ValueError("probe_attack_coverage_missing")
                evidence["input_attacks_rejected"] = len(rejected)
            elif not all(r["passed"] for r in rows) or value["passed"] != len(rows):
                raise ValueError("probe_failed")
            if name == "display" and (value["mock"] or not all(r["unknown_displayed"] for r in rows)):
                raise ValueError("real_display_probe_incomplete")
            evidence[name] = {"checks": len(rows), "sha256_utf8_lf": hashlib.sha256(raw).hexdigest()}
            if name == "recovery":
                evidence[name]["actual_sqlite_executions"] = value["actual_sqlite_executions"]
                evidence[name]["proposal"] = value["proposal"]
        report["public_in_process_probes"] = evidence
        report["not_proven"] = ["new_semantic_HTTP_API", "new_domain_database_execution", "semantic_F1",
                                "business_accuracy", "generalization", "migration_cost", "innovation", "product_release"]
    report["stable_sha256"] = digest(report)
    return report


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--tested-commit", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--component", action="append", default=[])
    parser.add_argument("--probes-dir", type=Path)
    args = parser.parse_args()
    ROOT = args.root.resolve()
    report = build_receipt(args.log, args.tested_commit, args.component, args.probes_dir)
    if args.check:
        if json.loads(args.report.read_text(encoding="utf-8")) != report:
            raise ValueError("receipt_drifted")
    else:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8", newline="\n")
    print(json.dumps({"status": "pass", "tests_run": report["unit_tests"]["run"],
                      "stable_sha256": report["stable_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
