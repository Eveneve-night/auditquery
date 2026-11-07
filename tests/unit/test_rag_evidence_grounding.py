"""规范化页文本精确对齐的离线单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.rag.evidence_grounding import (
    align_exact_text,
    grounding_audit_sha256,
)


class EvidenceGroundingTest(unittest.TestCase):
    def test_exact_chinese_quote_returns_half_open_interval(self):
        source = "项目说明 2025年度计划预算为人民币1200万元。 其他事项"
        quote = "2025年度计划预算为人民币1200万元。"
        grounding = align_exact_text(source, quote)
        self.assertIsNotNone(grounding)
        assert grounding is not None
        self.assertEqual(quote, source[grounding.char_start:grounding.char_end])
        self.assertEqual("exact", grounding.alignment)
        self.assertEqual(1, grounding.occurrence_count)
        self.assertEqual(64, len(grounding.normalized_text_sha256))
        self.assertEqual(64, len(grounding.quote_sha256))

    def test_repeated_quote_is_deterministic_and_records_ambiguity(self):
        source = "销售流程。补充说明。销售流程。"
        grounding = align_exact_text(source, "销售流程。")
        self.assertIsNotNone(grounding)
        assert grounding is not None
        self.assertEqual(0, grounding.char_start)
        self.assertEqual(2, grounding.occurrence_count)

        overlapping = align_exact_text("aaaa", "aa")
        self.assertIsNotNone(overlapping)
        assert overlapping is not None
        self.assertEqual(3, overlapping.occurrence_count)

    def test_missing_empty_and_non_string_quotes_fail_closed(self):
        self.assertIsNone(align_exact_text("已有原文", "伪造引文"))
        self.assertIsNone(align_exact_text("已有原文", ""))
        self.assertIsNone(align_exact_text("", "已有原文"))
        self.assertIsNone(align_exact_text("已有原文", None))  # type: ignore[arg-type]

    def test_audit_digest_is_stable_and_sensitive_to_interval(self):
        grounding = align_exact_text("甲乙丙", "乙")
        self.assertIsNotNone(grounding)
        assert grounding is not None
        record = grounding.to_audit_record(
            evidence_id="doc.demo.p1", document_id="doc.demo", page=1
        )
        first = grounding_audit_sha256([record])
        second = grounding_audit_sha256([dict(reversed(list(record.items())))])
        self.assertEqual(first, second)
        changed = dict(record)
        changed["char_start"] = 0
        self.assertNotEqual(first, grounding_audit_sha256([changed]))


if __name__ == "__main__":
    unittest.main()
