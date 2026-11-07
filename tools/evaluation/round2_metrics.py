"""Public development scorers. Never imported by request construction."""
from __future__ import annotations

import math
import re


def ranking_metrics(rankings, relevant):
    """Macro recall/MRR; exclude empty relevant sets with explicit counts."""
    if len(rankings) != len(relevant):
        raise ValueError("scoring_pair_count_mismatch")
    rows = []
    for ranked, gold in zip(rankings, relevant):
        if len(ranked) != len(set(ranked)) or len(gold) != len(set(gold)):
            raise ValueError("duplicate_scoring_id")
        if not gold:
            continue
        gold = set(gold)
        rows.append({**{f"recall_at_{k}": len(set(ranked[:k]) & gold) / len(gold) for k in (1, 3, 5)},
                     "mrr": next((1 / (i + 1) for i, item in enumerate(ranked) if item in gold), 0.0)})
    return {"scored_cases": len(rows), "empty_relevant_cases": len(relevant) - len(rows),
            **{k: sum(r[k] for r in rows) / len(rows) if rows else None
               for k in ("recall_at_1", "recall_at_3", "recall_at_5", "mrr")}}


def score_linking(pairs, scoring, pack):
    from backend.agent.round2_contracts import validate_linking_pair
    ids = [r["request"]["request_id"] for r in pairs]
    if len(ids) != len(set(ids)) or set(ids) != set(scoring):
        raise ValueError("scoring_case_set_mismatch")
    table_rank, column_rank, table_gold, column_gold = [], [], [], []
    table_ok, columns_ok, status_ok, false_selections = [], [], [], []
    for pair in pairs:
        request, result = pair["request"], pair["result"]
        validate_linking_pair(request, result, pack)
        labels = scoring[request["request_id"]]
        if set(labels) != {"table_ids", "column_ids", "status"}:
            raise ValueError("scoring_fields_invalid")
        if labels["status"] not in {"selected", "clarify", "abstain", "reject"} or (labels["status"] == "selected" and not labels["table_ids"]):
            raise ValueError("scoring_status_invalid")
        for field, kind in (("table_ids", "table"), ("column_ids", "column")):
            if any(ref not in request["authorized_object_ids"] or pack.resolve(ref)["object_type"] != kind for ref in labels[field]):
                raise ValueError("scoring_object_not_authorized")
        table_rank.append([x["object_id"] for x in result["table_candidates"]])
        column_rank.append([x["object_id"] for x in result["column_candidates"]])
        table_gold.append(labels["table_ids"])
        column_gold.append(labels["column_ids"])
        status_ok.append(result["status"] == labels["status"])
        if labels["status"] == "selected":
            table_ok.append(result["status"] == "selected" and result["selected_table_id"] in labels["table_ids"])
            columns_ok.append(table_ok[-1] and set(result["selected_column_ids"]) == set(labels["column_ids"]))
        else:
            false_selections.append(result["status"] == "selected")
    return {"scope": "public_development_not_business_accuracy", "cases": len(pairs),
            "tables": ranking_metrics(table_rank, table_gold), "columns": ranking_metrics(column_rank, column_gold),
            "final_table_accuracy": sum(table_ok) / len(table_ok) if table_ok else None,
            "final_table_and_columns_exact": sum(columns_ok) / len(columns_ok) if columns_ok else None,
            "positive_cases": len(table_ok), "status_accuracy": sum(status_ok) / len(status_ok) if status_ok else None,
            "selection_coverage": sum(r["result"]["status"] == "selected" for r in pairs) / len(pairs) if pairs else None,
            "nonselected_reference_cases": len(false_selections),
            "false_selection_rate": sum(false_selections) / len(false_selections) if false_selections else None,
            "abstain_cases": sum(r["result"]["status"] == "abstain" for r in pairs),
            "reject_cases": sum(r["result"]["status"] == "reject" for r in pairs)}


def compare_document_routes(records):
    """Paired measured outcomes only; this function never predicts complexity."""
    arms = {"uniform_pypdf", "uniform_pdfplumber", "adaptive"}
    grouped = {}
    for row in records:
        if set(row) != {"document_id", "document_sha256", "arm", "outcome", "preflight_ms", "parse_ms", "structure_score", "answer_support_score"}:
            raise ValueError("document_measurement_fields_invalid")
        if row["arm"] not in arms:
            raise ValueError("unknown_comparison_arm")
        if row["outcome"] not in {"succeeded", "failed", "rejected", "unsupported"}:
            raise ValueError("invalid_document_outcome")
        if (not isinstance(row["document_id"], str) or
            re.fullmatch(r"[a-z][a-z0-9_.:-]{0,127}", row["document_id"]) is None or
            not isinstance(row["document_sha256"], str) or
            re.fullmatch(r"[a-f0-9]{64}", row["document_sha256"]) is None):
            raise ValueError("invalid_document_identity")
        key = (row["document_id"], row["arm"])
        if key in grouped:
            raise ValueError("duplicate_document_arm")
        for name in ("preflight_ms", "parse_ms", "structure_score", "answer_support_score"):
            v = row[name]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError("invalid_document_measurement")
            if "score" in name and v > 1:
                raise ValueError("invalid_document_measurement")
        grouped[key] = row
    docs = {r[0] for r in grouped}
    hashes = []
    for doc in docs:
        rows = [grouped.get((doc, arm)) for arm in arms]
        if any(r is None for r in rows) or len({r["document_sha256"] for r in rows}) != 1:
            raise ValueError("unpaired_document_measurements")
        hashes.append(rows[0]["document_sha256"])
    if len(hashes) != len(set(hashes)):
        raise ValueError("duplicate_document_bytes")
    return {"scope": "public_paired_outcomes_not_prediction_inputs", "documents": len(docs),
            "minimum_document_count_met": len(docs) >= 30,
            "arms": {arm: {"outcomes": {outcome: sum(r["outcome"] == outcome for (d, a), r in grouped.items() if a == arm)
                                        for outcome in ("succeeded", "failed", "rejected", "unsupported")},
                           "mean_total_ms": sum(r["preflight_ms"] + r["parse_ms"] for (d, a), r in grouped.items() if a == arm) / len(docs) if docs else None,
                           "mean_structure_score": sum(r["structure_score"] for (d, a), r in grouped.items() if a == arm) / len(docs) if docs else None,
                           "mean_answer_support_score": sum(r["answer_support_score"] for (d, a), r in grouped.items() if a == arm) / len(docs) if docs else None}
                     for arm in sorted(arms)}}
