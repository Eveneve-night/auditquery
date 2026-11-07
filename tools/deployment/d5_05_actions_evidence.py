"""Generate and verify content-safe D5-05 GitHub Actions evidence.

The artifact is deliberately not a trust root.  Final Gate consumers must call
``verify-github`` so the repository, workflow, run id, attempt, head SHA and
successful job conclusion are independently bound to GitHub's Actions API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "d5-05-sandbox.yml"
SCHEMA_VERSION = "d5-05-actions-evidence.v1"
WORKFLOW_REPOSITORY_PATH = ".github/workflows/d5-05-sandbox.yml"
EXPECTED_WORKFLOW_NAME = "D5-05 isolated Docker and clean install"
EXPECTED_CHECKS = {
    "d5-05-clean-windows": {
        "locked_dependency_install",
        "pip_check",
        "powershell_51_probe",
    },
    "d5-05-clean-ubuntu": {
        "locked_dependency_install",
        "pip_check",
        "docker_build",
        "compose_up",
        "initial_readiness_fail_closed",
        "application_state_seeded",
        "compose_restart",
        "state_after_restart",
        "compose_down_up",
        "named_volume_state_after_recreate",
    },
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class EvidenceError(ValueError):
    """Stable, content-safe evidence validation failure."""


def _canonical_hash(payload: dict[str, object]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "stable_sha256"}
    encoded = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _workflow_hash(path: Path = WORKFLOW_PATH) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _required_environment(environ: dict[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise EvidenceError(f"missing_environment:{name}")
    return value


def build_evidence(
    *, job: str, checks: set[str], environ: dict[str, str], workflow_path: Path = WORKFLOW_PATH
) -> dict[str, object]:
    required_checks = EXPECTED_CHECKS.get(job)
    if required_checks is None:
        raise EvidenceError("unknown_job")
    if checks != required_checks:
        raise EvidenceError("incomplete_or_unknown_checks")

    head_sha = _required_environment(environ, "D5_EVIDENCE_HEAD_SHA")
    if not GIT_SHA_RE.fullmatch(head_sha):
        raise EvidenceError("invalid_head_sha")
    run_id = _required_environment(environ, "GITHUB_RUN_ID")
    run_attempt = _required_environment(environ, "GITHUB_RUN_ATTEMPT")
    if not run_id.isdigit() or not run_attempt.isdigit():
        raise EvidenceError("invalid_run_identity")

    event_name = _required_environment(environ, "GITHUB_EVENT_NAME")
    git_ref = _required_environment(environ, "GITHUB_REF")
    final_gate_eligible = event_name == "push" and git_ref == "refs/heads/main"
    evidence: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": f"{job}:{run_id}:{run_attempt}",
        "repository": _required_environment(environ, "GITHUB_REPOSITORY"),
        "workflow": {
            "name": _required_environment(environ, "GITHUB_WORKFLOW"),
            "path": WORKFLOW_REPOSITORY_PATH,
            "ref": _required_environment(environ, "GITHUB_WORKFLOW_REF"),
            "normalized_lf_sha256": _workflow_hash(workflow_path),
        },
        "run": {
            "id": int(run_id),
            "attempt": int(run_attempt),
            "event": event_name,
            "head_sha": head_sha,
            "ref": git_ref,
            "head_ref": environ.get("GITHUB_HEAD_REF", "").strip(),
        },
        "runner": {
            "job": job,
            "os": _required_environment(environ, "RUNNER_OS"),
            "arch": _required_environment(environ, "RUNNER_ARCH"),
            "image_os": environ.get("ImageOS", "").strip(),
            "image_version": environ.get("ImageVersion", "").strip(),
        },
        "checks": {name: True for name in sorted(checks)},
        "boundaries": {
            "model": "qwen3.7-flash",
            "model_api_calls": 0,
            "real_key_files_read": 0,
            "formal_validation_cases_read": 0,
            "hidden_test_cases_read": 0,
            "estimated_cost_cny": 0.0,
        },
        "trust": {
            "final_gate_eligible": final_gate_eligible,
            "self_asserted_artifact_is_sufficient": False,
            "required_verifier": "github_actions_api_v1",
        },
        "conclusion": "pass",
    }
    evidence["stable_sha256"] = _canonical_hash(evidence)
    return evidence


def validate_evidence(
    evidence: dict[str, object], *, workflow_path: Path = WORKFLOW_PATH
) -> None:
    if evidence.get("schema_version") != SCHEMA_VERSION:
        raise EvidenceError("schema_version_mismatch")
    stable_hash = evidence.get("stable_sha256")
    if not isinstance(stable_hash, str) or not SHA256_RE.fullmatch(stable_hash):
        raise EvidenceError("invalid_stable_hash")
    if stable_hash != _canonical_hash(evidence):
        raise EvidenceError("stable_hash_mismatch")

    workflow = evidence.get("workflow")
    run = evidence.get("run")
    runner = evidence.get("runner")
    checks = evidence.get("checks")
    boundaries = evidence.get("boundaries")
    trust = evidence.get("trust")
    if not all(isinstance(item, dict) for item in (workflow, run, runner, checks, boundaries, trust)):
        raise EvidenceError("invalid_evidence_shape")
    assert isinstance(workflow, dict) and isinstance(run, dict) and isinstance(runner, dict)
    assert isinstance(checks, dict) and isinstance(boundaries, dict) and isinstance(trust, dict)

    job = runner.get("job")
    if not isinstance(job, str) or set(checks) != EXPECTED_CHECKS.get(job):
        raise EvidenceError("check_set_mismatch")
    if any(value is not True for value in checks.values()):
        raise EvidenceError("check_not_passed")
    if workflow.get("name") != EXPECTED_WORKFLOW_NAME or workflow.get("path") != WORKFLOW_REPOSITORY_PATH:
        raise EvidenceError("workflow_identity_mismatch")
    if workflow.get("normalized_lf_sha256") != _workflow_hash(workflow_path):
        raise EvidenceError("workflow_hash_mismatch")
    if not isinstance(run.get("head_sha"), str) or not GIT_SHA_RE.fullmatch(str(run["head_sha"])):
        raise EvidenceError("invalid_head_sha")
    if evidence.get("conclusion") != "pass":
        raise EvidenceError("evidence_not_passed")
    expected_boundaries = {
        "model": "qwen3.7-flash",
        "model_api_calls": 0,
        "real_key_files_read": 0,
        "formal_validation_cases_read": 0,
        "hidden_test_cases_read": 0,
        "estimated_cost_cny": 0.0,
    }
    if boundaries != expected_boundaries:
        raise EvidenceError("boundary_mismatch")
    if trust.get("self_asserted_artifact_is_sufficient") is not False:
        raise EvidenceError("self_asserted_trust_forbidden")


def verify_against_run(
    evidence: dict[str, object],
    run_metadata: dict[str, object],
    *,
    expected_repository: str,
    expected_head_sha: str,
    require_main_push: bool,
    workflow_path: Path = WORKFLOW_PATH,
) -> None:
    validate_evidence(evidence, workflow_path=workflow_path)
    run = evidence["run"]
    trust = evidence["trust"]
    assert isinstance(run, dict) and isinstance(trust, dict)
    repository = run_metadata.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != expected_repository:
        raise EvidenceError("github_repository_mismatch")
    if evidence.get("repository") != expected_repository:
        raise EvidenceError("artifact_repository_mismatch")
    if run_metadata.get("status") != "completed" or run_metadata.get("conclusion") != "success":
        raise EvidenceError("github_run_not_successful")
    if str(run_metadata.get("id")) != str(run.get("id")):
        raise EvidenceError("github_run_id_mismatch")
    if int(run_metadata.get("run_attempt", 0)) != int(run.get("attempt", 0)):
        raise EvidenceError("github_run_attempt_mismatch")
    if run_metadata.get("head_sha") != expected_head_sha or run.get("head_sha") != expected_head_sha:
        raise EvidenceError("github_head_sha_mismatch")
    if run_metadata.get("path") != WORKFLOW_REPOSITORY_PATH:
        raise EvidenceError("github_workflow_path_mismatch")
    if run_metadata.get("event") != run.get("event"):
        raise EvidenceError("github_event_mismatch")
    if require_main_push:
        if (
            run_metadata.get("event") != "push"
            or run_metadata.get("head_branch") != "main"
            or run.get("ref") != "refs/heads/main"
            or trust.get("final_gate_eligible") is not True
        ):
            raise EvidenceError("protected_main_push_required")


def _fetch_run_metadata(repository: str, run_id: int, token: str) -> dict[str, object]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ict-agent-d5-evidence-verifier",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed API origin
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise EvidenceError("invalid_github_run_payload")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成或验证 D5-05 Actions 机器证据")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--job", choices=sorted(EXPECTED_CHECKS), required=True)
    generate.add_argument("--check", action="append", default=[])
    generate.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify-github")
    verify.add_argument("--evidence", type=Path, required=True)
    verify.add_argument("--repository", required=True)
    verify.add_argument("--run-id", type=int, required=True)
    verify.add_argument("--expected-head-sha", required=True)
    verify.add_argument("--require-main-push", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "generate":
            evidence = build_evidence(job=args.job, checks=set(args.check), environ=dict(os.environ))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            print(json.dumps({"status": "pass", "stable_sha256": evidence["stable_sha256"]}))
            return 0

        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise EvidenceError("missing_github_token")
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        metadata = _fetch_run_metadata(args.repository, args.run_id, token)
        verify_against_run(
            evidence,
            metadata,
            expected_repository=args.repository,
            expected_head_sha=args.expected_head_sha,
            require_main_push=args.require_main_push,
        )
        print(json.dumps({"status": "pass", "run_id": args.run_id}))
        return 0
    except (EvidenceError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "fail", "error_code": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
