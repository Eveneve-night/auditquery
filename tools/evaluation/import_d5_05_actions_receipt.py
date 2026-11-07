#!/usr/bin/env python3
"""Import trusted D5-05 GitHub Actions evidence into a content-safe receipt.

The importer is the online trust step.  It downloads artifacts from the exact
GitHub Actions run, verifies each artifact against the GitHub run API, and
writes a stable receipt.  The final Gate consumes that receipt offline; a
non-author reviewer must rerun this importer before approving a new receipt.
"""

from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.deployment.d5_05_actions_evidence import (  # noqa: E402
    EXPECTED_CHECKS,
    EvidenceError,
    verify_against_run,
)


RECEIPT_SCHEMA_VERSION = "d5-05-trusted-actions-receipt.v1"
DEFAULT_OUTPUT = PROJECT_ROOT / "tests" / "reports" / "d5-05-trusted-actions-evidence-v1.json"
# Retained receipt-validation compatibility, outside the product CI.
# An operator must explicitly select a repository; no private repository default.
EXPECTED_REPOSITORY = os.environ.get("AUDITQUERY_EVIDENCE_REPOSITORY", "")
EXPECTED_WORKFLOW_PATH = ".github/workflows/d5-05-sandbox.yml"
EXPECTED_RUNNER_OS = {
    "d5-05-clean-ubuntu": "Linux",
    "d5-05-clean-windows": "Windows",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_ARTIFACT_BYTES = 1024 * 1024


class ReceiptError(ValueError):
    """Stable, content-safe receipt validation error."""


def _canonical_hash(payload: dict[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "stable_sha256"}
    encoded = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _github_request(url: str, token: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ict-agent-d5-receipt-importer",
        },
    )


def _github_json(url: str, token: str) -> dict[str, object]:
    with urllib.request.urlopen(_github_request(url, token), timeout=30) as response:  # noqa: S310
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ReceiptError("invalid_github_payload")
    return payload


def _github_bytes(url: str, token: str) -> bytes:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(_github_request(url, token), timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code not in {301, 302, 303, 307, 308}:
            raise
        location = exc.headers.get("Location", "")
    else:
        raise ReceiptError("artifact_download_redirect_required")
    parsed = urllib.parse.urlparse(location)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".blob.core.windows.net"):
        raise ReceiptError("invalid_artifact_redirect")
    # The Azure URL carries a short-lived SAS signature.  Never forward the
    # GitHub bearer token to a different host.
    with urllib.request.urlopen(location, timeout=30) as response:  # noqa: S310
        payload = response.read(MAX_ARTIFACT_BYTES + 1)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ReceiptError("artifact_archive_too_large")
    return payload


def _extract_evidence(archive: bytes) -> dict[str, object]:
    try:
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            names = [name for name in bundle.namelist() if not name.endswith("/")]
            if len(names) != 1 or Path(names[0]).name != names[0] or not names[0].endswith(".json"):
                raise ReceiptError("invalid_artifact_file_set")
            info = bundle.getinfo(names[0])
            if info.file_size > MAX_ARTIFACT_BYTES:
                raise ReceiptError("artifact_payload_too_large")
            raw = bundle.read(info)
            if len(raw) > MAX_ARTIFACT_BYTES:
                raise ReceiptError("artifact_payload_too_large")
            payload = json.loads(raw.decode("utf-8"))
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("invalid_artifact_archive") from exc
    if not isinstance(payload, dict):
        raise ReceiptError("invalid_evidence_payload")
    return payload


def build_receipt(
    *, repository: str, run_id: int, expected_head_sha: str, token: str
) -> dict[str, object]:
    if repository != EXPECTED_REPOSITORY:
        raise ReceiptError("unexpected_repository")
    if not GIT_SHA_RE.fullmatch(expected_head_sha):
        raise ReceiptError("invalid_expected_head_sha")
    api_root = f"https://api.github.com/repos/{repository}/actions"
    run = _github_json(f"{api_root}/runs/{run_id}", token)
    artifacts_payload = _github_json(f"{api_root}/runs/{run_id}/artifacts?per_page=100", token)
    artifacts = artifacts_payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReceiptError("invalid_artifact_list")

    jobs: list[dict[str, object]] = []
    seen_jobs: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReceiptError("invalid_artifact_metadata")
        name = artifact.get("name")
        matched_job = next(
            (job for job in EXPECTED_CHECKS if name == f"{job}-evidence-{run_id}-{run.get('run_attempt')}"),
            None,
        )
        if matched_job is None:
            continue
        if matched_job in seen_jobs:
            raise ReceiptError("duplicate_job_artifact")
        if artifact.get("expired") is not False:
            raise ReceiptError("artifact_expired")
        artifact_size = artifact.get("size_in_bytes")
        if type(artifact_size) is not int or not 0 < artifact_size <= MAX_ARTIFACT_BYTES:
            raise ReceiptError("invalid_artifact_size")
        download_url = artifact.get("archive_download_url")
        if not isinstance(download_url, str) or not download_url.startswith(
            f"https://api.github.com/repos/{repository}/actions/artifacts/"
        ):
            raise ReceiptError("invalid_artifact_download_url")
        archive = _github_bytes(download_url, token)
        archive_sha256 = hashlib.sha256(archive).hexdigest()
        api_digest = artifact.get("digest")
        if api_digest != f"sha256:{archive_sha256}":
            raise ReceiptError("artifact_digest_mismatch")
        evidence = _extract_evidence(archive)
        try:
            verify_against_run(
                evidence,
                run,
                expected_repository=repository,
                expected_head_sha=expected_head_sha,
                require_main_push=True,
            )
        except EvidenceError as exc:
            raise ReceiptError(str(exc)) from exc
        runner = evidence.get("runner")
        checks = evidence.get("checks")
        workflow = evidence.get("workflow")
        if not isinstance(runner, dict) or not isinstance(checks, dict) or not isinstance(workflow, dict):
            raise ReceiptError("invalid_verified_evidence")
        jobs.append(
            {
                "job": matched_job,
                "runner": runner,
                "checks": checks,
                "workflow_normalized_lf_sha256": workflow.get("normalized_lf_sha256"),
                "evidence_stable_sha256": evidence.get("stable_sha256"),
                "artifact": {
                    "id": artifact.get("id"),
                    "name": name,
                    "api_url": artifact.get("url"),
                    "archive_digest": api_digest,
                    "archive_size_bytes": artifact.get("size_in_bytes"),
                },
            }
        )
        seen_jobs.add(matched_job)

    if seen_jobs != set(EXPECTED_CHECKS):
        raise ReceiptError("required_job_artifact_missing")
    jobs.sort(key=lambda item: str(item["job"]))
    repository_info = run.get("repository")
    if not isinstance(repository_info, dict) or repository_info.get("full_name") != repository:
        raise ReceiptError("github_repository_mismatch")
    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "source": {
            "repository": repository,
            "run_id": run_id,
            "run_attempt": run.get("run_attempt"),
            "run_url": run.get("html_url"),
            "event": run.get("event"),
            "branch": run.get("head_branch"),
            "head_sha": run.get("head_sha"),
            "workflow_path": run.get("path"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
        },
        "jobs": jobs,
        "verification": {
            "method": "github_actions_api_v1",
            "main_push_required": True,
            "artifacts_downloaded_and_digest_matched": True,
            "self_asserted_artifact_is_sufficient": False,
        },
        "boundaries": {
            "model": "qwen3.7-flash",
            "model_api_calls": 0,
            "real_key_files_read": 0,
            "formal_validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "estimated_cost_cny": 0.0,
        },
    }
    receipt["stable_sha256"] = _canonical_hash(receipt)
    validate_receipt(receipt, expected_head_sha=expected_head_sha)
    return receipt


def validate_receipt(receipt: dict[str, object], *, expected_head_sha: str | None = None) -> None:
    if set(receipt) != {
        "schema_version", "source", "jobs", "verification", "boundaries", "stable_sha256"
    }:
        raise ReceiptError("unknown_receipt_field")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ReceiptError("receipt_schema_version_mismatch")
    stable_hash = receipt.get("stable_sha256")
    if not isinstance(stable_hash, str) or not SHA256_RE.fullmatch(stable_hash):
        raise ReceiptError("invalid_receipt_hash")
    if stable_hash != _canonical_hash(receipt):
        raise ReceiptError("receipt_hash_mismatch")
    source = receipt.get("source")
    verification = receipt.get("verification")
    jobs = receipt.get("jobs")
    boundaries = receipt.get("boundaries")
    if not isinstance(source, dict) or not isinstance(verification, dict):
        raise ReceiptError("invalid_receipt_shape")
    if not isinstance(jobs, list) or not isinstance(boundaries, dict):
        raise ReceiptError("invalid_receipt_shape")
    if set(source) != {
        "repository", "run_id", "run_attempt", "run_url", "event", "branch", "head_sha",
        "workflow_path", "status", "conclusion"
    }:
        raise ReceiptError("unknown_receipt_source_field")
    if source != {
        "repository": EXPECTED_REPOSITORY,
        "run_id": source.get("run_id"),
        "run_attempt": source.get("run_attempt"),
        "run_url": f"https://github.com/{EXPECTED_REPOSITORY}/actions/runs/{source.get('run_id')}",
        "event": "push",
        "branch": "main",
        "head_sha": source.get("head_sha"),
        "workflow_path": EXPECTED_WORKFLOW_PATH,
        "status": "completed",
        "conclusion": "success",
    }:
        raise ReceiptError("invalid_receipt_source")
    if type(source.get("run_id")) is not int or type(source.get("run_attempt")) is not int:
        raise ReceiptError("invalid_receipt_run_identity")
    if source["run_id"] <= 0 or source["run_attempt"] <= 0:
        raise ReceiptError("invalid_receipt_run_identity")
    head_sha = source.get("head_sha")
    if not isinstance(head_sha, str) or not GIT_SHA_RE.fullmatch(head_sha):
        raise ReceiptError("invalid_receipt_head_sha")
    if expected_head_sha is not None and head_sha != expected_head_sha:
        raise ReceiptError("receipt_head_sha_mismatch")
    if verification != {
        "method": "github_actions_api_v1",
        "main_push_required": True,
        "artifacts_downloaded_and_digest_matched": True,
        "self_asserted_artifact_is_sufficient": False,
    }:
        raise ReceiptError("invalid_receipt_verification")
    expected_boundaries = {
        "model": "qwen3.7-flash",
        "model_api_calls": 0,
        "real_key_files_read": 0,
        "formal_validation_cases_read": 0,
        "hidden_test_cases_read": 0,
        "estimated_cost_cny": 0.0,
    }
    if boundaries != expected_boundaries:
        raise ReceiptError("receipt_boundary_mismatch")
    if len(jobs) != len(EXPECTED_CHECKS):
        raise ReceiptError("receipt_job_count_mismatch")
    seen: set[str] = set()
    workflow_hashes: set[str] = set()
    for item in jobs:
        if not isinstance(item, dict):
            raise ReceiptError("invalid_receipt_job")
        if set(item) != {
            "job", "runner", "checks", "workflow_normalized_lf_sha256",
            "evidence_stable_sha256", "artifact"
        }:
            raise ReceiptError("unknown_receipt_job_field")
        job = item.get("job")
        checks = item.get("checks")
        runner = item.get("runner")
        artifact = item.get("artifact")
        if not isinstance(job, str) or job in seen or job not in EXPECTED_CHECKS:
            raise ReceiptError("invalid_receipt_job_identity")
        if not isinstance(checks, dict) or set(checks) != EXPECTED_CHECKS[job] or any(
            value is not True for value in checks.values()
        ):
            raise ReceiptError("invalid_receipt_checks")
        if not isinstance(runner, dict) or runner.get("job") != job:
            raise ReceiptError("invalid_receipt_runner")
        if set(runner) != {"job", "os", "arch", "image_os", "image_version"}:
            raise ReceiptError("unknown_receipt_runner_field")
        if runner.get("os") != EXPECTED_RUNNER_OS[job]:
            raise ReceiptError("invalid_receipt_runner_os")
        if not isinstance(artifact, dict) or set(artifact) != {
            "id", "name", "api_url", "archive_digest", "archive_size_bytes"
        }:
            raise ReceiptError("invalid_receipt_artifact")
        if type(artifact.get("id")) is not int or artifact["id"] <= 0:
            raise ReceiptError("invalid_receipt_artifact")
        if artifact.get("name") != f"{job}-evidence-{source['run_id']}-{source['run_attempt']}":
            raise ReceiptError("invalid_receipt_artifact_name")
        if artifact.get("api_url") != (
            f"https://api.github.com/repos/{EXPECTED_REPOSITORY}/actions/artifacts/{artifact['id']}"
        ):
            raise ReceiptError("invalid_receipt_artifact_url")
        if type(artifact.get("archive_size_bytes")) is not int or not 0 < artifact["archive_size_bytes"] <= MAX_ARTIFACT_BYTES:
            raise ReceiptError("invalid_receipt_artifact_size")
        digest = artifact.get("archive_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:") or not SHA256_RE.fullmatch(digest[7:]):
            raise ReceiptError("invalid_receipt_artifact_digest")
        evidence_hash = item.get("evidence_stable_sha256")
        workflow_hash = item.get("workflow_normalized_lf_sha256")
        if not isinstance(evidence_hash, str) or not SHA256_RE.fullmatch(evidence_hash):
            raise ReceiptError("invalid_receipt_evidence_hash")
        if not isinstance(workflow_hash, str) or not SHA256_RE.fullmatch(workflow_hash):
            raise ReceiptError("invalid_receipt_workflow_hash")
        workflow_hashes.add(workflow_hash)
        seen.add(job)
    workflow_bytes = (PROJECT_ROOT / EXPECTED_WORKFLOW_PATH).read_bytes().replace(b"\r\n", b"\n")
    current_workflow_hash = hashlib.sha256(workflow_bytes).hexdigest()
    if seen != set(EXPECTED_CHECKS) or workflow_hashes != {current_workflow_hash}:
        raise ReceiptError("receipt_job_set_mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 D5-05 可信 GitHub Actions 证据")
    parser.add_argument("--repository", default=EXPECTED_REPOSITORY)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise ReceiptError("missing_github_token")
        receipt = build_receipt(
            repository=args.repository,
            run_id=args.run_id,
            expected_head_sha=args.expected_head_sha,
            token=token,
        )
        if args.check:
            if not args.output.is_file() or json.loads(args.output.read_text(encoding="utf-8")) != receipt:
                raise ReceiptError("committed_receipt_drifted")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        print(json.dumps({"status": "pass", "stable_sha256": receipt["stable_sha256"]}))
        return 0
    except (ReceiptError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "fail", "error_code": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
