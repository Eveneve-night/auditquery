"""Input-only public D4-08 case source; no model or benchmark labels."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from backend.evaluation.d4_runner import D4EvaluationError  # noqa: E402
from backend.evaluation.public_input_source import PinnedPublicInputSource  # noqa: E402


CASE_ID = "public.case-001"
CATALOG_SHA256 = "c" * 64


def _row(input_value=None):
    return {
        "case_id": CASE_ID,
        "input": input_value or {
            "current_request": "synthetic public question",
            "candidate_options": ["catalog.metric", "catalog.document"],
        },
    }


class PublicInputSourceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "public-input.jsonl"

    def make_source(self, payload):
        raw = payload if isinstance(payload, bytes) else (
            json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        )
        self.path.write_bytes(raw)
        return PinnedPublicInputSource(
            self.path, sha256=hashlib.sha256(raw).hexdigest(),
            case_ids=(CASE_ID,), candidate_catalog_sha256=CATALOG_SHA256,
        )

    def test_input_only_public_row_is_loaded_as_defensive_copy(self):
        source = self.make_source(_row())
        first = source.load_input(CASE_ID)
        first["candidate_options"].append("injected")
        self.assertEqual(["catalog.metric", "catalog.document"],
                         source.load_input(CASE_ID)["candidate_options"])
        self.assertEqual(CATALOG_SHA256, source.candidate_catalog_sha256)
        with self.assertRaisesRegex(D4EvaluationError, "public_input_case_unknown"):
            source.load_input("public.case-002")

    def test_raw_benchmark_row_with_answer_is_rejected(self):
        row = _row()
        row["answer"] = "hidden answer"
        with self.assertRaisesRegex(D4EvaluationError, "public_input_row_fields_invalid"):
            self.make_source(row)

    def test_nested_answer_and_unknown_input_field_are_rejected(self):
        row = _row()
        row["input"]["evidence_refs"] = [{"gold_answer": "hidden"}]
        with self.assertRaisesRegex(D4EvaluationError, "answer_field_in_case_input"):
            self.make_source(row)
        row = _row()
        row["input"]["private_note"] = "unapproved"
        with self.assertRaisesRegex(D4EvaluationError, "public_input_fields_invalid"):
            self.make_source(row)

    def test_hash_and_case_id_must_match_approved_values(self):
        source = self.make_source(_row())
        self.assertIsNotNone(source)
        with self.assertRaisesRegex(D4EvaluationError, "public_input_hash_mismatch"):
            PinnedPublicInputSource(
                self.path, sha256="0" * 64, case_ids=(CASE_ID,),
                candidate_catalog_sha256=CATALOG_SHA256,
            )
        raw = self.path.read_bytes()
        with self.assertRaisesRegex(D4EvaluationError, "public_input_case_ids_mismatch"):
            PinnedPublicInputSource(
                self.path, sha256=hashlib.sha256(raw).hexdigest(),
                case_ids=("public.case-002",), candidate_catalog_sha256=CATALOG_SHA256,
            )

    def test_duplicate_keys_and_nonfinite_numbers_fail_closed(self):
        for raw in (
            b'{"case_id":"public.case-001","case_id":"public.case-001","input":{}}\n',
            b'{"case_id":"public.case-001","input":{"current_request":NaN}}\n',
        ):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(D4EvaluationError, "public_input_jsonl_invalid"):
                    self.make_source(raw)

    def test_blank_line_and_oversized_artifact_fail_closed(self):
        with self.assertRaisesRegex(D4EvaluationError, "public_input_jsonl_invalid"):
            self.make_source(json.dumps(_row()).encode("utf-8") + b"\n\n")
        with self.assertRaisesRegex(D4EvaluationError, "public_input_size_invalid"):
            self.make_source(b" " * (2 * 1024 * 1024 + 1))


if __name__ == "__main__":
    unittest.main()
