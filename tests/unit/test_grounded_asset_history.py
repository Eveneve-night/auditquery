"""Retained legacy asset assertions; requires excluded evaluation corpus."""
from test_grounded_answer_contract import *

class GroundedAssetHistoryTest(GroundedAnswerContractTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rag_cases = [
            json.loads(line)
            for line in GOLDEN_RAG_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_candidate_selection_matches_goldens_and_fixtures(self) -> None:
        selected = self.candidates["selected_case_ids"]
        self.assertEqual("prompt_dev", self.candidates["split"])
        self.assertEqual(5, len(selected))
        self.assertEqual(len(set(selected)), len(selected))

        cases_by_id = {case["id"]: case for case in self.rag_cases}
        for case_id in selected:
            self.assertIn(case_id, cases_by_id, f"{case_id} 不在金标准文件中")
            case = cases_by_id[case_id]
            self.assertEqual("prompt_dev", case["split"], f"{case_id} 不属于 prompt_dev 拆分")

        abstain_ids = {
            case_id
            for case_id in selected
            if cases_by_id[case_id]["expected"]["should_abstain"]
        }
        self.assertEqual({"RAG-009"}, abstain_ids, "必须且只需包含 prompt_dev 唯一拒答用例 RAG-009")

        documents = {cases_by_id[case_id]["context"]["document_id"] for case_id in selected}
        self.assertGreaterEqual(len(documents), 2, "候选应覆盖至少 2 份文档")

        for case_id in selected:
            case = cases_by_id[case_id]
            document_id = case["context"]["document_id"]
            self.assertIn(document_id, self.fixture_evidence, f"{document_id} 无对应证据夹具")
            provided = set(case["context"]["evidence_ids"])
            supporting = set(case["expected"]["supporting_evidence_ids"])
            self.assertTrue(
                provided <= self.fixture_evidence[document_id],
                f"{case_id}: context 引用了夹具中不存在的证据",
            )
            self.assertTrue(
                supporting <= provided,
                f"{case_id}: supporting 证据未被提供",
            )

        prompt_dev_documents = {
            case["context"]["document_id"]
            for case in self.rag_cases
            if case["split"] == "prompt_dev"
        }
        self.assertNotIn(
            "project_report",
            prompt_dev_documents,
            "candidates notes 声称 prompt_dev 无 project_report 用例，需与事实一致",
        )
        for backup in self.candidates["coverage"]["backup_candidates"]:
            self.assertIn(backup, cases_by_id, f"备选用例 {backup} 不存在")

    def test_no_gold_queries_leak_into_prompt_or_candidates(self) -> None:
        """金标题目整句不得进入 Prompt 或候选文件。

        docs/23 红线 2：validation/test 的题目、答案、证据不得进入 Prompt。
        上一轮只锁了答案论断（required_claims），题目本身同样不能抄。
        """
        candidates_text = json.dumps(self.candidates, ensure_ascii=False)
        for case in self.rag_cases:
            query = case["input"]["query"]
            with self.subTest(case=case["id"]):
                self.assertNotIn(query, self.system_prompt)
                self.assertNotIn(query, candidates_text)

    def test_no_gold_answer_phrases_leak_into_prompt_or_candidates(self) -> None:
        """金标答案措辞不得抄回 Prompt 或候选文件（禁止用 expected 构造输入）。

        长度阈值说明（2026-09-25）：v1 资产中有 2 条 2-3 字的纯情态论断
        （RAG-015「不能」、RAG-013「不可以」），不含事实信息，用子串匹配必然与
        中文指令文字撞车产生假阳性。因此只对长度 >= 4 的论断做全量扫描，
        同时断言被跳过的条目不超过 2 条——若以后出现新的超短论断，
        本用例会失败并强制人工复核，避免阈值悄悄放宽。
        """
        candidates_text = json.dumps(self.candidates, ensure_ascii=False)
        skipped: list[tuple[str, str]] = []
        for case in self.rag_cases:
            for claim in case["expected"].get("required_claims", []):
                if len(claim) < MIN_CLAIM_LENGTH_FOR_LEAK_SCAN:
                    skipped.append((case["id"], claim))
                    continue
                with self.subTest(case=case["id"], claim=claim):
                    self.assertNotIn(claim, self.system_prompt)
                    self.assertNotIn(claim, candidates_text)

        self.assertLessEqual(
            len(skipped),
            MAX_SKIPPED_SHORT_CLAIMS,
            f"超短论断数量 {skipped} 超出冻结值，需人工复核后更新阈值",
        )

    def test_known_document_family_overlap_matches_goldens(self) -> None:
        """机器检出文档族跨 split：实际重叠必须与声明逐项一致（不多、不少、不腐化）。

        docs/23 第 4.1 节要求 RAG 按文档族拆分，同一文档的相邻问法不得分处不同 split。
        v1 资产存在冻结期遗留的历史重叠（人工评审才发现过一次，属于 C 的盲区），
        这里锁成「声明即契约」双向断言：
        - 出现任何未声明的新重叠 → 失败；
        - 声明的重叠已修好却没删除 → 同样失败，防止声明烂掉后假装问题还在或已解决。
        """
        actual: dict[str, dict[str, list[str]]] = {}
        for case in self.rag_cases:
            document_id = case.get("context", {}).get("document_id")
            self.assertIsNotNone(document_id, f"{case['id']} 缺少 context.document_id")
            actual.setdefault(document_id, {}).setdefault(case["split"], []).append(case["id"])

        overlapping = {doc: splits for doc, splits in actual.items() if len(splits) > 1}

        block = self.candidates["split_discipline"]["document_family_overlap"]
        declared = {entry["document_id"]: entry for entry in block["known"]}
        self.assertEqual(
            len(block["known"]),
            len(declared),
            "document_family_overlap.known 存在重复 document_id",
        )

        undeclared = sorted(set(overlapping) - set(declared))
        self.assertEqual([], undeclared, f"出现未声明的文档族跨 split: {undeclared}")

        stale = sorted(set(declared) - set(overlapping))
        self.assertEqual(
            [],
            stale,
            f"声明的重叠已不复现: {stale}；请删除声明，让检测回归「零重叠」",
        )

        for document_id, splits in sorted(overlapping.items()):
            entry = declared[document_id]
            with self.subTest(document=document_id):
                self.assertEqual(sorted(splits), sorted(entry["splits"]))
                for split_name, case_ids in splits.items():
                    self.assertEqual(
                        sorted(case_ids),
                        sorted(entry["splits"][split_name]),
                        f"{document_id}/{split_name} 的用例集合与声明不一致",
                    )
                self.assertTrue(entry.get("owner"), "声明必须写明整改负责人")
                self.assertTrue(entry.get("remediation"), "声明必须写明整改动作")
