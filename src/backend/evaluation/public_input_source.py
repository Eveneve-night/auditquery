"""Pinned, input-only public cases for an independently approved D4 canary.

The preparer must export a separate JSONL containing only ``case_id`` and
``input``.  Raw benchmark rows with answers must never be passed here.  This
source does not choose cases, define a split, score results, or call a model.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .d4_adapter import _INPUT_FIELDS, _reject_answer_fields
from .d4_runner import D4EvaluationError


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,95}\Z")
_MAX_BYTES = 2 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("non_finite_json_number")


def _validate_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value or set(value) - _INPUT_FIELDS:
        raise D4EvaluationError("public_input_fields_invalid")
    _reject_answer_fields(value)
    request = value.get("current_request")
    candidates = value.get("candidate_options")
    if not isinstance(request, str) or not request.strip() or len(request) > 4000:
        raise D4EvaluationError("public_request_invalid")
    if (not isinstance(candidates, list) or not 1 <= len(candidates) <= 100
            or any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in candidates)
            or len(set(candidates)) != len(candidates)):
        raise D4EvaluationError("public_candidates_invalid")
    refs = value.get("evidence_refs")
    if refs is not None and (not isinstance(refs, list) or len(refs) > 100
                            or any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in refs)
                            or len(set(refs)) != len(refs)):
        raise D4EvaluationError("public_evidence_refs_invalid")
    version = value.get("domain_pack_version")
    if version is not None and (not isinstance(version, str) or not version.strip() or len(version) > 128):
        raise D4EvaluationError("public_domain_pack_version_invalid")
    return value


class PinnedPublicInputSource:
    """Load an exact-hash, answer-free public input artifact by approved ID."""

    def __init__(self, path: Path, *, sha256: str, case_ids: tuple[str, ...],
                 candidate_catalog_sha256: str) -> None:
        if (not _SHA256.fullmatch(sha256) or not _SHA256.fullmatch(candidate_catalog_sha256)
                or not case_ids or len(set(case_ids)) != len(case_ids)
                or any(not _CASE_ID.fullmatch(case_id) for case_id in case_ids)):
            raise D4EvaluationError("public_input_identity_invalid")
        try:
            file_path = Path(path)
            if file_path.stat().st_size > _MAX_BYTES:
                raise D4EvaluationError("public_input_size_invalid")
            raw = file_path.read_bytes()
        except OSError:
            raise D4EvaluationError("public_input_unavailable") from None
        if not raw or len(raw) > _MAX_BYTES or hashlib.sha256(raw).hexdigest() != sha256:
            raise D4EvaluationError("public_input_hash_mismatch")
        try:
            lines = raw.decode("utf-8").splitlines()
            cases: dict[str, dict[str, Any]] = {}
            for line in lines:
                if not line.strip():
                    raise ValueError("blank_jsonl_line")
                row = json.loads(line, object_pairs_hook=_unique_object,
                                 parse_constant=_invalid_constant)
                if not isinstance(row, dict) or set(row) != {"case_id", "input"}:
                    raise D4EvaluationError("public_input_row_fields_invalid")
                case_id = row["case_id"]
                if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id) or case_id in cases:
                    raise D4EvaluationError("public_input_case_id_invalid")
                cases[case_id] = _validate_input(row["input"])
        except D4EvaluationError:
            raise
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise D4EvaluationError("public_input_jsonl_invalid") from None
        if tuple(cases) != case_ids:
            raise D4EvaluationError("public_input_case_ids_mismatch")
        self.candidate_catalog_sha256 = candidate_catalog_sha256
        self._cases = cases

    def load_input(self, case_id: str) -> Mapping[str, Any]:
        if case_id not in self._cases:
            raise D4EvaluationError("public_input_case_unknown")
        return deepcopy(self._cases[case_id])


__all__ = ["PinnedPublicInputSource"]
