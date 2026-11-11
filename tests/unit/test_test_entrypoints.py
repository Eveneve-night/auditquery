"""Guards for the two offline test entry points.

The entry points declare file patterns instead of importing test modules, so a
renamed or deleted test file silently removes coverage. These checks fail when a
declared pattern stops resolving, when the core and specialized entries overlap,
or when a skipped suite is reported as a success.
"""
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
TESTS_DIR = ROOT / "tests/unit"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRODUCT = _load("entrypoint_test_product", TOOLS / "test_product.py")
SPECIALIZED = _load("entrypoint_test_specialized", TOOLS / "test_specialized.py")


class DeclaredPatternTests(unittest.TestCase):
    def test_core_patterns_resolve_to_one_file(self):
        for pattern in PRODUCT.PATTERNS:
            matches = SPECIALIZED.resolve_pattern(pattern)
            self.assertEqual(len(matches), 1, f"core pattern {pattern} matched {len(matches)} files")

    def test_specialized_patterns_resolve_to_one_file(self):
        for pattern in SPECIALIZED.all_patterns():
            matches = SPECIALIZED.resolve_pattern(pattern)
            self.assertEqual(len(matches), 1, f"specialized pattern {pattern} matched {len(matches)} files")

    def test_no_pattern_is_covered_by_both_entries(self):
        overlap = set(PRODUCT.PATTERNS) & set(SPECIALIZED.all_patterns())
        self.assertEqual(overlap, set(), f"patterns declared twice: {sorted(overlap)}")

    def test_suite_metadata_is_complete(self):
        for name, spec in SPECIALIZED.SUITES.items():
            self.assertTrue(spec["description"], f"{name} needs a description")
            self.assertTrue(spec["patterns"], f"{name} needs at least one pattern")
            unknown = set(spec["requires"]) - set(SPECIALIZED.KNOWN_REQUIREMENTS)
            self.assertEqual(unknown, set(), f"{name} declares unknown prerequisites {sorted(unknown)}")


class RunnerBehaviourTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOLS / "test_specialized.py"), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_list_mode_prints_every_suite_and_exits_zero(self):
        result = self._run("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in SPECIALIZED.SUITES:
            self.assertIn(name, result.stdout)

    def test_unknown_suite_is_a_usage_error(self):
        result = self._run("--suite", "no-such-suite")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_selected_suite_runs_and_reports_a_summary(self):
        result = self._run("--suite", "round2-schema-linking")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("All 1 suite(s) passed", result.stdout)

    def test_unresolvable_pattern_fails_instead_of_shrinking_coverage(self):
        broken = {"broken": {"description": "guards the guard", "patterns": ("test_does_not_exist.py",), "requires": ()}}
        with mock.patch.dict(SPECIALIZED.SUITES, broken, clear=True):
            code = SPECIALIZED.main(["--suite", "broken"])
        self.assertEqual(code, SPECIALIZED.EXIT_FAILED)

    def test_missing_prerequisite_is_incomplete_not_passed(self):
        with mock.patch.object(SPECIALIZED, "missing_requirements", return_value=["pdfplumber"]):
            strict = SPECIALIZED.main(["--suite", "round2-document"])
            lenient = SPECIALIZED.main(["--suite", "round2-document", "--allow-skip"])
        self.assertEqual(strict, SPECIALIZED.EXIT_INCOMPLETE)
        self.assertEqual(lenient, SPECIALIZED.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
