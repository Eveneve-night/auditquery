"""Inventory the first-batch main/candidate boundary without inventing a pass.

Only Git object metadata is read during preparation. No credential, dataset
content, model, or network access is used. This is not a release Gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
EXCLUDED = frozenset({
    "tests/reports/batch1-postmerge-stage-v1.json",
    "tests/reports/batch1-postmerge-validation-v1.json",
    "docs/协作/当前任务板.md",
    "docs/协作/A-角色信息同步板.md",
    "docs/协作/第一批合并后验收.md",
    "docs/tasks/BATCH1-A-公共语义契约v1.md",
})


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def source_manifest(root: Path, revision: str):
    result = {}
    for entry in git(root, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not entry:
            continue
        metadata, path_raw = entry.split(b"\t", 1)
        path = path_raw.decode("utf-8")
        if path in EXCLUDED:
            continue
        mode, kind, oid = metadata.decode("ascii").split()
        result[path] = {"mode": mode, "kind": kind, "git_oid": oid}
    return dict(sorted(result.items()))


def compare(before, after):
    return {key: {"before": before.get(key), "after": after.get(key)}
            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}


def verify_receipt(receipt, revision: str):
    payload = dict(receipt)
    reported = payload.pop("stable_sha256")
    if sha(payload) != reported or receipt["tested_commit"] != revision:
        raise ValueError("validation_revision_or_digest_mismatch")
    unit = receipt["unit_tests"]
    ids = unit["test_ids"]
    if (not ids or len(set(ids)) != len(ids) or
        not all(unit[k] == len(ids) for k in ("discovered", "run", "passed")) or
        unit["failed"] or unit["errors"] or unit["skipped"] or unit["import_errors"] or
        sha(ids) != unit["test_ids_sha256"]):
        raise ValueError("validation_incomplete")
    if any(receipt["boundaries"][key] != 0 for key in (
        "model_api_calls", "real_key_reads", "formal_validation_content_reads", "hidden_test_reads", "cost_cny"
    )):
        raise ValueError("validation_boundary_violation")


def verified_probes(directory: Path):
    result = {}
    for name in ("seed", "c-check", "adapter", "display", "recovery"):
        raw = (directory / (name + ".json")).read_bytes().replace(b"\r\n", b"\n")
        value = json.loads(raw)
        summary = {"sha256_utf8_lf": hashlib.sha256(raw).hexdigest()}
        if name == "seed":
            if value["status"] != "pass" or value["total_cases"] != 60 or value["model_api_calls"] or value["network_calls"]:
                raise ValueError("seed_evidence_incomplete")
            summary["cases"] = value["total_cases"]
        elif name == "c-check":
            if value != {"status": "pass", "report_version": "2.0.0"}:
                raise ValueError("C_report_check_failed")
            summary["report_version"] = value["report_version"]
        else:
            if any(value[k] != 0 for k in ("model_calls", "key_reads", "cost_cny")):
                raise ValueError("probe_boundary_violation")
            rows = value["rows"] if name != "recovery" else value["checks"]
            if not rows:
                raise ValueError("empty_probe")
            if name == "adapter":
                if (value["status"] != "public_path_checked" or value["public_adapter_status"] != "executed" or
                    len(value["results"]) != len(rows) or
                    not all(r["request_valid"] and r["B_output_valid"] for r in rows) or
                    set(value["unsafe_request_attacks_rejected"]) != {"expected", "gold", "label", "required_slots", "catalog_escape"}):
                    raise ValueError("adapter_evidence_incomplete")
                summary["input_attacks_rejected"] = len(value["unsafe_request_attacks_rejected"])
            elif value["passed"] != len(rows) or not all(r["passed"] for r in rows):
                raise ValueError("probe_failed")
            if name == "display" and (value["mock"] or not all(r["unknown_displayed"] for r in rows)):
                raise ValueError("actual_display_evidence_missing")
            summary["checks"] = len(rows)
            if name == "recovery":
                summary["actual_sqlite_executions"] = value["actual_sqlite_executions"]
                summary["proposal"] = value["proposal"]
        result[name] = summary
    return result


def build_report(root: Path, baseline: str, revision: str, receipt=None, evidence_dir: Path | None = None):
    baseline = git(root, "rev-parse", baseline).decode().strip()
    revision = git(root, "rev-parse", revision).decode().strip()
    subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", baseline, revision], check=True)
    inputs = source_manifest(root, revision)
    report = {
        "report_version": "1.0.0",
        "scope": "batch1_postmerge_not_release_gate",
        "main_baseline": baseline,
        "evaluated": {"kind": "main" if baseline == revision else "candidate_not_main",
                      "commit": revision, "tree": git(root, "rev-parse", revision + "^{tree}").decode().strip()},
        "source_manifest": inputs,
        "source_manifest_sha256": sha(inputs),
        "source_hash_scope": "all_tracked_git_objects_including_fixtures_tests_reports_startup_and_dependencies",
        "excluded_self_referential_outputs_and_A_governance": sorted(EXCLUDED),
        "baseline_delta": compare(source_manifest(root, baseline), inputs),
        "validation": {"status": "not_run_waiting_for_stable_candidate"},
        "stage_conclusion": "pending_current_tree_validation_and_C_report_consistency",
        "not_proven": ["formal_semantic_F1", "model_business_accuracy", "hidden_generalization",
                       "freeze_before_migration", "person_hours", "new_semantic_HTTP",
                       "new_domain_database_execution", "innovation", "product_release"],
        "boundaries": {"model_api_calls": 0, "real_key_reads": 0,
                       "formal_validation_content_reads": 0, "hidden_test_reads": 0, "cost_cny": 0},
    }
    if receipt is not None:
        verify_receipt(receipt, revision)
        report["validation"] = {"status": "recorded_for_this_revision",
                                "receipt_stable_sha256": receipt["stable_sha256"],
                                "tests_run": receipt["unit_tests"]["run"]}
    if evidence_dir is not None:
        if receipt is None:
            raise ValueError("probes_without_revision_bound_full_suite")
        report["public_offline_evidence"] = verified_probes(evidence_dir)
        report["stage_conclusion"] = ("candidate_public_offline_pass_not_main_review_and_merge_pending"
                                      if baseline != revision else "main_public_offline_pass_not_release")
        # Authored local artifacts are not CI attestations or GitHub review
        # approval. Source binding does not itself authorize merge/release.
    report["stable_sha256"] = sha(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--main-baseline", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8")) if args.receipt else None
    report = build_report(args.root, args.main_baseline, args.revision, receipt, args.evidence_dir)
    if args.check:
        if json.loads(args.report.read_text(encoding="utf-8")) != report:
            raise ValueError("postmerge_report_drifted")
    else:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"evaluated": report["evaluated"], "validation": report["validation"],
                      "stable_sha256": report["stable_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
