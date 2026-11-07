"""NEXT-02：逐事实引用与标题/同名误命中防护的单元测试。

离线运行：不调用模型、不连网、不读取 Key。覆盖回答组装模块的四个承诺：
逐事实编号、长度预算内不截断单条事实、标题误命中必须排除、无证据则拒答。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from backend.rag.answer_composition import (
    ANSWER_MODE_EXTRACTIVE,
    ANSWER_MODE_MODEL,
    MAX_MESSAGE_CHARS,
    compose_extractive_message,
    is_message_within_limit,
    is_title_only_evidence,
    select_answer_facts,
)

# shared-api-definitions.v2 对 warnings 元素的约束。
WARNING_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

ABSTAIN_MESSAGE = "检索证据不足，系统已拒绝生成答案。"


def _citation(snippet: str, *, title: str = "已发布文档", page: int = 1,
              score: float = 2.0, doc: str = "doc-1") -> dict:
    return {
        "evidence_id": f"{doc}.p{page}",
        "document_id": doc,
        "document_title": title,
        "page": page,
        "snippet": snippet,
        "score": score,
    }


class AnswerCompositionTest(unittest.TestCase):
    """逐事实引用与拒答边界。"""

    def test_multiple_facts_are_numbered_in_citation_order(self):
        facts = [_citation("第一事实"), _citation("第二事实", page=2)]
        self.assertEqual(compose_extractive_message(facts), "第一事实 [1] 第二事实 [2]")

    def test_empty_facts_abstain_with_explicit_message(self):
        self.assertEqual(select_answer_facts([]), [])
        self.assertEqual(compose_extractive_message([]), ABSTAIN_MESSAGE)

    def test_budget_never_truncates_a_single_fact(self):
        long_fact = "甲" * 200
        facts = select_answer_facts([
            _citation(long_fact),
            _citation("乙" * 200, page=2),
        ])
        # 第二条整体放不下 → 放弃，绝不截断成半句。
        self.assertEqual(len(facts), 1)
        message = compose_extractive_message(facts)
        self.assertLessEqual(len(message), MAX_MESSAGE_CHARS)
        self.assertIn(long_fact, message)

    def test_facts_fitting_budget_are_all_kept(self):
        facts = select_answer_facts([
            _citation("短事实一"),
            _citation("短事实二", page=2),
            _citation("短事实三", page=3),
        ])
        self.assertEqual(len(facts), 3)
        self.assertEqual(
            compose_extractive_message(facts),
            "短事实一 [1] 短事实二 [2] 短事实三 [3]",
        )


class TitleCollisionTest(unittest.TestCase):
    """标题 / 同名误命中防护。"""

    def test_snippet_equal_to_title_is_not_evidence(self):
        self.assertTrue(is_title_only_evidence("年度报告", "年度报告"))

    def test_punctuation_only_difference_still_collides(self):
        self.assertTrue(is_title_only_evidence("年度报告。", "年度报告"))

    def test_snippet_that_merely_contains_title_is_evidence(self):
        self.assertFalse(is_title_only_evidence("年度报告共三章内容", "年度报告"))

    def test_truncated_title_prefix_is_not_evidence(self):
        self.assertTrue(
            is_title_only_evidence("年度报告二零二六年", "年度报告二零二六年第一季度")
        )

    def test_title_only_hit_leads_to_abstention(self):
        citations = [_citation("年度报告", title="年度报告")]
        self.assertEqual(select_answer_facts(citations), [])
        self.assertEqual(
            compose_extractive_message(select_answer_facts(citations)),
            ABSTAIN_MESSAGE,
        )

    def test_real_body_after_title_hit_is_still_kept(self):
        citations = [
            _citation("年度报告", title="年度报告"),
            _citation("本年度营收为一千万元", page=2, title="年度报告"),
        ]
        facts = select_answer_facts(citations)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["snippet"], "本年度营收为一千万元")

    def test_blank_snippet_is_dropped(self):
        self.assertEqual(select_answer_facts([_citation("   ")]), [])


class AnswerModeMarkerTest(unittest.TestCase):
    """摘录 / 模型模式分清（模式标记需符合冻结 schema）。"""

    def test_markers_are_distinct(self):
        self.assertNotEqual(ANSWER_MODE_EXTRACTIVE, ANSWER_MODE_MODEL)

    def test_marker_matches_warning_schema_pattern(self):
        self.assertRegex(ANSWER_MODE_EXTRACTIVE, WARNING_PATTERN)
        self.assertRegex(ANSWER_MODE_MODEL, WARNING_PATTERN)

    def test_message_limit_check(self):
        self.assertTrue(is_message_within_limit("短"))
        self.assertFalse(is_message_within_limit(""))
        self.assertFalse(is_message_within_limit("x" * 257))


if __name__ == "__main__":
    unittest.main()
