from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.deployment.d5_05_actions_evidence import (  # noqa: E402
    EXPECTED_CHECKS,
    EvidenceError,
    _canonical_hash,
    build_evidence,
    validate_evidence,
    verify_against_run,
)


HEAD_SHA = "a" * 40
REPOSITORY = "example/auditquery"


def environment(*, event: str = "pull_request", ref: str = "refs/pull/86/merge") -> dict[str, str]:
    return {
        "D5_EVIDENCE_HEAD_SHA": HEAD_SHA,
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKFLOW": "D5-05 isolated Docker and clean install",
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/d5-05-sandbox.yml@refs/heads/main",
        "GITHUB_RUN_ID": "38554610706",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF": ref,
        "GITHUB_HEAD_REF": "feature/example" if event == "pull_request" else "",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "ImageOS": "ubuntu24",
        "ImageVersion": "20261001.1",
    }


def run_metadata(*, event: str = "pull_request", branch: str = "feature/example") -> dict[str, object]:
    return {
        "id": 38554610706,
        "run_attempt": 2,
        "status": "completed",
        "conclusion": "success",
        "head_sha": HEAD_SHA,
        "head_branch": branch,
        "event": event,
        "path": ".github/workflows/d5-05-sandbox.yml",
        "repository": {"full_name": REPOSITORY},
    }


class D505ActionsEvidenceTests(unittest.TestCase):
    def build(self, job: str = "d5-05-clean-ubuntu", **kwargs):
        return build_evidence(
            job=job,
            checks=set(EXPECTED_CHECKS[job]),
            environ=environment(**kwargs),
        )

    def test_docker_evidence_has_required_checks_and_stable_hash(self):
        evidence = self.build()
        validate_evidence(evidence)
        self.assertEqual(set(evidence["checks"]), EXPECTED_CHECKS["d5-05-clean-ubuntu"])
        self.assertEqual(evidence["boundaries"]["model_api_calls"], 0)
        self.assertFalse(evidence["trust"]["self_asserted_artifact_is_sufficient"])

    def test_windows_evidence_has_clean_install_checks(self):
        evidence = self.build("d5-05-clean-windows")
        validate_evidence(evidence)
        self.assertEqual(set(evidence["checks"]), EXPECTED_CHECKS["d5-05-clean-windows"])

    def test_incomplete_or_unknown_check_is_rejected(self):
        checks = set(EXPECTED_CHECKS["d5-05-clean-ubuntu"])
        checks.remove("docker_build")
        with self.assertRaisesRegex(EvidenceError, "incomplete_or_unknown_checks"):
            build_evidence(job="d5-05-clean-ubuntu", checks=checks, environ=environment())

    def test_tampered_artifact_is_rejected(self):
        evidence = self.build()
        evidence["checks"]["docker_build"] = False
        with self.assertRaisesRegex(EvidenceError, "stable_hash_mismatch"):
            validate_evidence(evidence)

    def test_rehashed_forged_pass_is_not_enough_without_matching_github_run(self):
        evidence = self.build()
        evidence["run"]["head_sha"] = "b" * 40
        evidence["stable_sha256"] = _canonical_hash(evidence)
        with self.assertRaisesRegex(EvidenceError, "github_head_sha_mismatch"):
            verify_against_run(
                evidence,
                run_metadata(),
                expected_repository=REPOSITORY,
                expected_head_sha=HEAD_SHA,
                require_main_push=False,
            )

    def test_pr_artifact_cannot_satisfy_protected_main_gate(self):
        evidence = self.build()
        with self.assertRaisesRegex(EvidenceError, "protected_main_push_required"):
            verify_against_run(
                evidence,
                run_metadata(),
                expected_repository=REPOSITORY,
                expected_head_sha=HEAD_SHA,
                require_main_push=True,
            )

    def test_main_push_artifact_binds_to_successful_github_run(self):
        evidence = self.build(event="push", ref="refs/heads/main")
        verify_against_run(
            evidence,
            run_metadata(event="push", branch="main"),
            expected_repository=REPOSITORY,
            expected_head_sha=HEAD_SHA,
            require_main_push=True,
        )
        self.assertTrue(evidence["trust"]["final_gate_eligible"])

    def test_failed_or_wrong_run_is_rejected(self):
        evidence = self.build()
        metadata = run_metadata()
        metadata["conclusion"] = "failure"
        with self.assertRaisesRegex(EvidenceError, "github_run_not_successful"):
            verify_against_run(
                evidence,
                metadata,
                expected_repository=REPOSITORY,
                expected_head_sha=HEAD_SHA,
                require_main_push=False,
            )

    def test_workflow_hash_uses_lf_normalization(self):
        content = "name: test\non: push\n"
        with tempfile.TemporaryDirectory() as directory:
            lf = Path(directory) / "lf.yml"
            crlf = Path(directory) / "crlf.yml"
            lf.write_bytes(content.encode("utf-8"))
            crlf.write_bytes(content.replace("\n", "\r\n").encode("utf-8"))
            left = build_evidence(
                job="d5-05-clean-windows",
                checks=set(EXPECTED_CHECKS["d5-05-clean-windows"]),
                environ=environment(),
                workflow_path=lf,
            )
            right = build_evidence(
                job="d5-05-clean-windows",
                checks=set(EXPECTED_CHECKS["d5-05-clean-windows"]),
                environ=environment(),
                workflow_path=crlf,
            )
            self.assertEqual(
                left["workflow"]["normalized_lf_sha256"],
                right["workflow"]["normalized_lf_sha256"],
            )

    def test_workflow_is_sustainable_and_uploads_both_artifacts(self):
        workflow = (ROOT / ".github/workflows/d5-05-sandbox.yml").read_text(encoding="utf-8")
        self.assertNotIn("github.head_ref ==", workflow)
        self.assertIn("  push:\n", workflow)
        self.assertIn("  workflow_dispatch:\n", workflow)
        self.assertEqual(workflow.count("uses: actions/upload-artifact@v4"), 2)
        self.assertIn("d5-05-clean-windows-evidence-${{ github.run_id }}", workflow)
        self.assertIn("d5-05-clean-ubuntu-evidence-${{ github.run_id }}", workflow)


if __name__ == "__main__":
    unittest.main()
