"""D5-05 合并后 A Gate 的离线契约与失败分层。"""

from copy import deepcopy
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "evaluation" / "validate_d5_05_postmerge_gate.py"
SPEC = importlib.util.spec_from_file_location("d5_postmerge_gate", TOOL)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(mod)


SOURCE_BINDING = {
    "schema_version": "d5-gate-evaluated-source.v1",
    "evaluated_commit_sha": "a" * 40,
    "evaluated_tree_oid": "b" * 40,
    "binding_rule": "evaluated_commit_ancestor_and_complete_source_manifest_equal",
    "report_self_reference_excluded": True,
    "included_prefixes": ["src/", "tests/"],
    "included_top_level_files": [],
    "excluded_paths": ["tests/reports/d5-05-postmerge-gate-v2.json"],
    "file_count": 1,
    "files": [{"path": "src/example.py", "mode": "100644", "blob_oid": "c" * 40}],
    "source_manifest_stable_sha256": "d" * 64,
}


class D505PostmergeGateTests(unittest.TestCase):
    def test_hash_is_stable_and_excludes_no_fields(self) -> None:
        value = {"b": [2, 1], "a": "中文"}
        self.assertEqual(mod._canonical_hash(value), mod._canonical_hash(deepcopy(value)))
        changed = deepcopy(value)
        changed["b"].append(3)
        self.assertNotEqual(mod._canonical_hash(value), mod._canonical_hash(changed))

    def test_missing_docker_is_blocking_not_pass(self) -> None:
        with patch.object(mod.shutil, "which", return_value=None):
            result = mod._docker_probe()
        self.assertEqual("blocked", result["status"])
        self.assertEqual("docker_cli_missing", result["reason"])
        self.assertEqual(0, result["commands_executed"])

    def test_windows_parse_failure_is_stable_and_safe(self) -> None:
        completed = type("Completed", (), {"returncode": 2})()
        with patch.object(mod.os, "name", "nt"), patch.object(mod.shutil, "which", return_value="powershell"), \
                patch.object(mod.subprocess, "run", return_value=completed):
            result = mod._windows_probe(Path("python.exe"))
        self.assertEqual("fail", result["status"])
        self.assertEqual("windows_powershell_parse_error", result["error_code"])
        self.assertFalse(result["start_reached"])
        self.assertNotIn("stderr", result)

    def test_report_never_contains_secret_or_overclaims(self) -> None:
        full = {"status": "pass", "tests_run": 1, "failures": 0}
        seed = {"status": "pass", "total_cases": 60, "model_api_calls": 0, "network_calls": 0}
        runtime = {
            "localhost_http_started": True,
            "visitor_home_without_login": True,
            "admin_v2_optional_password": True,
            "multipart_upload": True,
            "document_publish": True,
            "fake_secret_saved_without_echo": True,
            "process_restart": True,
            "admin_persisted": True,
            "document_persisted": True,
            "index_version_persisted": True,
            "secret_configured_persisted": True,
            "admin_health_after_restart": True,
            "backup_restore": True,
            "snapshot_excludes_master_key": True,
            "wrong_master_key_fails_closed": True,
            "fake_key_plaintext_absent": True,
            "password_plaintext_absent": True,
            "offline_query_fails_closed": True,
        }
        visitor = {
            "status": "pass", "actual_http": True, "published_document_retrieved": True,
            "citation_reference_valid": True, "same_session_five_turn": True,
            "turns_completed": 5, "clarification_round_trip": True,
            "no_evidence_fails_closed": True, "model_api_calls": 0, "localhost_only": True,
        }
        with patch.object(mod, "_run_tests", return_value=full), \
                patch.object(mod, "_seed_probe", return_value=seed), \
                patch.object(mod, "_runtime_persistence_probe", return_value=runtime), \
                patch.object(mod, "_visitor_product_probe", return_value=visitor), \
                patch.object(mod, "_windows_probe", return_value={"status": "fail"}), \
                patch.object(mod, "_docker_probe", return_value={"status": "blocked"}), \
                patch.object(mod, "_verify_evaluated_source", return_value=SOURCE_BINDING), \
                patch.object(mod, "_is_ancestor", return_value=True), \
                patch.object(mod, "_git", return_value="a" * 40):
            report = mod.build_report(run_tests=True)
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertEqual("blocked", report["status"])
        self.assertFalse(report["gate_decision"]["d5_05_complete"])
        self.assertFalse(report["gate_decision"]["d5_06_admitted"])
        self.assertNotIn(mod.FAKE_API_KEY, encoded)
        contract = json.loads(mod.BOOTSTRAP_PATH.read_text(encoding="utf-8"))
        self.assertNotIn(contract["initial_credentials"]["initial_password"], encoded)
        self.assertEqual(0, report["boundaries"]["model_api_calls"])

    def test_trusted_main_actions_close_local_docker_gap(self) -> None:
        full = {"status": "pass", "tests_run": 1, "failures": 0}
        seed = {"status": "pass", "total_cases": 60, "model_api_calls": 0, "network_calls": 0}
        runtime = {"runtime_ok": True}
        visitor = {
            "status": "pass", "actual_http": True, "published_document_retrieved": True,
            "same_session_five_turn": True, "turns_completed": 5,
            "clarification_round_trip": True, "model_api_calls": 0,
        }
        with patch.object(mod, "_run_tests", return_value=full), \
                patch.object(mod, "_seed_probe", return_value=seed), \
                patch.object(mod, "_runtime_persistence_probe", return_value=runtime), \
                patch.object(mod, "_visitor_product_probe", return_value=visitor), \
                patch.object(mod, "_windows_probe", return_value={"status": "pass"}), \
                patch.object(mod, "_docker_probe", return_value={"status": "blocked"}), \
                patch.object(mod, "_trusted_actions_probe", return_value={"status": "pass"}), \
                patch.object(mod, "_verify_evaluated_source", return_value=SOURCE_BINDING), \
                patch.object(mod, "_is_ancestor", return_value=True), \
                patch.object(mod, "_git", return_value="a" * 40):
            report = mod.build_report(run_tests=True)
        self.assertEqual([], report["gate_decision"]["blockers"])
        self.assertEqual("pass", report["status"])
        self.assertTrue(report["gate_decision"]["d5_05_complete"])
        self.assertEqual("blocked", report["deployment"]["local_docker_observation"]["status"])
        self.assertEqual("pass", report["deployment"]["docker_compose"]["status"])
        self.assertEqual("pass", report["five_turn_evidence"]["d5_product_http_five_turn"])
        self.assertNotIn("windows_start_restart", report["not_proven"])
        self.assertNotIn("published_document_visitor_rag", report["not_proven"])

    def test_missing_trusted_actions_remains_blocking(self) -> None:
        full = {"status": "pass", "tests_run": 1, "failures": 0}
        seed = {"status": "pass", "total_cases": 60, "model_api_calls": 0, "network_calls": 0}
        visitor = {
            "status": "pass", "actual_http": True, "published_document_retrieved": True,
            "same_session_five_turn": True, "turns_completed": 5,
            "clarification_round_trip": True, "model_api_calls": 0,
        }
        with patch.object(mod, "_run_tests", return_value=full), \
                patch.object(mod, "_seed_probe", return_value=seed), \
                patch.object(mod, "_runtime_persistence_probe", return_value={"runtime_ok": True}), \
                patch.object(mod, "_visitor_product_probe", return_value=visitor), \
                patch.object(mod, "_windows_probe", return_value={"status": "pass"}), \
                patch.object(mod, "_docker_probe", return_value={"status": "blocked"}), \
                patch.object(mod, "_trusted_actions_probe", return_value={"status": "fail"}), \
                patch.object(mod, "_verify_evaluated_source", return_value=SOURCE_BINDING), \
                patch.object(mod, "_is_ancestor", return_value=True):
            report = mod.build_report(run_tests=True)
        self.assertEqual(["trusted_clean_runner_evidence_not_passed"], report["gate_decision"]["blockers"])
        self.assertFalse(report["gate_decision"]["d5_06_admitted"])

    def test_visitor_probe_runs_real_http_without_report_content(self) -> None:
        result = mod._visitor_product_probe()
        self.assertEqual("pass", result["status"])
        self.assertTrue(result["actual_http"])
        self.assertTrue(result["published_document_retrieved"])
        self.assertTrue(result["same_session_five_turn"])
        self.assertEqual(5, result["turns_completed"])
        encoded = json.dumps(result, ensure_ascii=False)
        for private_content in ("销售流程", "统计艺术家总数", "月度经营报表", "区块链"):
            self.assertNotIn(private_content, encoded)

    def test_committed_report_cannot_be_built_with_skipped_tests(self) -> None:
        with patch.object(mod, "build_report") as build_report, redirect_stdout(StringIO()):
            result = mod.main(["--skip-tests"])
        self.assertEqual(2, result)
        build_report.assert_not_called()

    def test_real_source_manifest_covers_every_required_category(self) -> None:
        manifest = mod._source_manifest_for_commit("HEAD")
        paths = {item["path"] for item in manifest["files"]}
        self.assertTrue(set(mod.REQUIRED_SOURCE_PATHS).issubset(paths))
        self.assertNotIn(mod.REPORT_PATH.relative_to(ROOT).as_posix(), paths)
        self.assertGreater(manifest["file_count"], 250)

    def test_runtime_frontend_config_fixture_and_test_mutations_are_rejected(self) -> None:
        tracked = {
            "src/backend/product/server.py": "runtime-v1\n",
            "src/frontend/d5-console/index.html": "frontend-v1\n",
            "configs/contracts/d5/product-layer.v1.json": "{}\n",
            "tests/fixtures/d5-05/probe.txt": "fixture-v1\n",
            "tests/unit/test_product.py": "test-v1\n",
        }
        for mutated_path in tracked:
            with self.subTest(path=mutated_path), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for relative, content in tracked.items():
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8", newline="\n")
                self._git(root, "init", "-q")
                self._git(root, "add", ".")
                self._commit(root, "baseline")
                baseline = self._git(root, "rev-parse", "HEAD").stdout.strip()
                path = root / mutated_path
                path.write_text(path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8", newline="\n")
                self._git(root, "add", mutated_path)
                self._commit(root, "mutation")
                with self._source_repo(root, tuple(tracked)):
                    with self.assertRaisesRegex(mod.GateProbeError, "evaluated_source_changed"):
                        mod._verify_evaluated_source(baseline)

    def test_report_only_commit_does_not_create_self_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src" / "backend" / "product" / "server.py"
            source.parent.mkdir(parents=True)
            source.write_text("runtime-v1\n", encoding="utf-8", newline="\n")
            self._git(root, "init", "-q")
            self._git(root, "add", ".")
            self._commit(root, "source")
            evaluated = self._git(root, "rev-parse", "HEAD").stdout.strip()
            report = root / "tests" / "reports" / "d5-05-postmerge-gate-v2.json"
            report.parent.mkdir(parents=True)
            report.write_text("{}\n", encoding="utf-8", newline="\n")
            self._git(root, "add", ".")
            self._commit(root, "report")
            with self._source_repo(root, ("src/backend/product/server.py",)):
                binding = mod._verify_evaluated_source(evaluated)
            self.assertEqual(evaluated, binding["evaluated_commit_sha"])
            self.assertNotIn(
                "tests/reports/d5-05-postmerge-gate-v2.json",
                {item["path"] for item in binding["files"]},
            )

    def test_check_stops_before_probes_when_source_binding_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report.write_text(
                json.dumps({"evaluated_source": {"evaluated_commit_sha": "a" * 40}}),
                encoding="utf-8",
            )
            with patch.object(
                mod, "_verify_evaluated_source", side_effect=mod.GateProbeError("evaluated_source_changed")
            ), patch.object(mod, "build_report") as build_report, redirect_stdout(StringIO()) as output:
                result = mod.main(["--check", "--report", str(report)])
        self.assertEqual(1, result)
        build_report.assert_not_called()
        self.assertIn("evaluated_source_changed", output.getvalue())

    @staticmethod
    def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            encoding="utf-8", check=True,
        )

    @classmethod
    def _commit(cls, root: Path, message: str) -> None:
        cls._git(
            root, "-c", "user.name=Gate Test", "-c", "user.email=gate@example.invalid",
            "commit", "-q", "-m", message,
        )

    @staticmethod
    def _source_repo(root: Path, required: tuple[str, ...]):
        return _SourceRepoPatch(root, required)


class _SourceRepoPatch:
    def __init__(self, root: Path, required: tuple[str, ...]) -> None:
        self._patches = [
            patch.object(mod, "PROJECT_ROOT", root),
            patch.object(mod, "SOURCE_MANIFEST_PREFIXES", ("src/", "configs/", "tests/")),
            patch.object(mod, "SOURCE_MANIFEST_TOP_LEVEL_FILES", ()),
            patch.object(
                mod, "SOURCE_MANIFEST_EXCLUDED_PATHS",
                ("tests/reports/d5-05-postmerge-gate-v2.json",),
            ),
            patch.object(mod, "REQUIRED_SOURCE_PATHS", required),
        ]

    def __enter__(self):
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        for item in reversed(self._patches):
            item.stop()
        return False


if __name__ == "__main__":
    unittest.main()
