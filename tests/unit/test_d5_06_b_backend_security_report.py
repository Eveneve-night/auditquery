"""Pure checks for the scoped D5-06-B report generator."""

import sys
import types
import unittest
from unittest.mock import patch

from tools.evaluation import validate_d5_06_b_backend_security as report_module
from tools.evaluation.validate_d5_06_b_backend_security import (
    assert_offline_environment, assert_sanitized, percentile, resource_limits,
    run_unit_matrix,
)


class D506BReportTests(unittest.TestCase):
    def test_percentile_interpolation(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(percentile([10], 0.95), 10)
        self.assertAlmostEqual(percentile([0, 10], 0.95), 9.5)

    def test_percentile_rejects_invalid_inputs(self):
        for values, fraction in (([], 0.5), ([1], -0.1), ([1], 1.1)):
            with self.subTest(values=values, fraction=fraction):
                with self.assertRaises(ValueError):
                    percentile(values, fraction)

    def test_report_rejects_secret_markers(self):
        assert_sanitized('{"status":"pass"}')
        for marker in ("sk-testonly", "ict_admin_session", "csrf_token"):
            with self.subTest(marker=marker):
                with self.assertRaises(ValueError):
                    assert_sanitized('{"unexpected":"' + marker + '"}')

    def test_external_secret_environment_fails_closed(self):
        with patch.dict("os.environ", {"ICT_MASTER_KEY_FILE": "C:/outside/key.txt"}):
            with self.assertRaises(RuntimeError):
                assert_offline_environment()

    def test_resource_limits_are_enforced_configuration_not_benchmark_guess(self):
        limits = resource_limits()
        self.assertEqual(limits["sql_max_rows"], 100)
        self.assertEqual(limits["sql_max_elapsed_ms"], 2000)
        self.assertEqual(limits["loop_max_model_calls"], 4)
        self.assertEqual(limits["policy_version"], "1.0.0")

    def test_unit_matrix_does_not_import_tests_namespace_package(self):
        third_party_tests = types.ModuleType("tests")
        third_party_tests.__path__ = ["C:/site-packages/tests"]
        with patch.dict(sys.modules, {"tests": third_party_tests}), \
                patch.object(report_module, "TEST_FILES", ("test_d4_sql_safety.py",)), \
                patch.object(unittest.TestLoader, "loadTestsFromNames", side_effect=AssertionError("namespace import")):
            matrix = run_unit_matrix()
        self.assertEqual(matrix["cases"], 25)
        self.assertEqual(matrix["run"], 25)
        self.assertTrue(matrix["passed"])
