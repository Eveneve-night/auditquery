"""D4-07 strict offline demo API and static-asset application.

The public API accepts only versioned scenario identifiers.  It cannot accept
arbitrary SQL, model parameters, evidence, or answer-derived fields.  SQL demo
scenarios still traverse D4-04/D4-05 and D4-02 before the readonly executor;
RAG scenarios expose deterministic D4-06 evidence retrieval without claiming
that a model answer was generated.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from ..agent.clarification_recovery import (
    ClarificationRecoveryError,
    D4ClarificationRecoveryRuntime,
    SqlProposal,
    build_clarification_answer,
)
from ..agent.domain_pack import DomainPackRegistry, canonical_text_sha256
from ..agent.intent_routing import (
    apply_intent_result,
    create_conversation_state,
    load_source_catalog,
)
from ..agent.selective_routing import build_slot_observations
from ..rag.lexical_retriever import BM25Retriever
from ..rag.pdf_parser import cross_validate


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCENARIOS_PATH = (
    PROJECT_ROOT / "configs" / "demo" / "d4-07" / "public-scenarios.v1.json"
)
DEFAULT_SCHEMA_ROOT = PROJECT_ROOT / "configs" / "schemas" / "d4-07"
DEFAULT_FRONTEND_ROOT = PROJECT_ROOT / "src" / "frontend" / "d4-demo"
DEFAULT_PDF_MANIFEST = PROJECT_ROOT / "data" / "manifests" / "pdf-v1.json"
DEMO_VERSION = "1.0.0"
DEMO_MODE = "offline_public_demo"
MAX_REQUEST_BYTES = 4096
RECORDED_AT = "2026-09-29T08:00:00Z"
EVIDENCE_PATTERN = re.compile(r"^\[([A-Z]+-[0-9]{3})\]\s*(.+)$")
_FORBIDDEN_KEYS = {"label", "labels", "answer", "answers", "target_answer"}


class D4DemoError(RuntimeError):
    """Content-safe API failure with a stable public code and HTTP status."""

    def __init__(self, code: str, http_status: int = 400) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass
class _StaticProposalPort:
    proposal: SqlProposal
    calls: int = 0

    def propose(self, *, conversation_state, slot_observations):
        self.calls += 1
        if not conversation_state or not slot_observations:
            raise D4DemoError("offline_proposal_context_missing", 500)
        return self.proposal


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise D4DemoError(code, 500) from exc


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _project_file(project_root: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    candidate = Path(normalized)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise D4DemoError("project_path_outside_root", 500)
    resolved = (project_root / candidate).resolve()
    if resolved != project_root and project_root not in resolved.parents:
        raise D4DemoError("project_path_outside_root", 500)
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_pdf_document(
    project_root: Path, document: Mapping[str, Any]
) -> tuple[str, dict[str, int]]:
    """Validate one frozen PDF and its D4-06 evidence-to-page mapping."""

    required = {
        "document_id",
        "title",
        "source_markdown",
        "pdf_path",
        "sha256",
        "bytes",
        "page_count",
        "evidence_pages",
    }
    if not required.issubset(document):
        raise D4DemoError("invalid_pdf_manifest", 500)
    pdf_path_value = str(document["pdf_path"]).replace("\\", "/")
    if not re.fullmatch(r"data/raw/pdf/[a-z0-9_]+\.pdf", pdf_path_value):
        raise D4DemoError("invalid_pdf_path", 500)
    expected_hash = document["sha256"]
    expected_bytes = document["bytes"]
    expected_page_count = document["page_count"]
    pages = document["evidence_pages"]
    if (
        not isinstance(expected_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        or type(expected_bytes) is not int
        or expected_bytes <= 0
        or type(expected_page_count) is not int
        or expected_page_count <= 0
        or not isinstance(pages, Mapping)
        or not pages
    ):
        raise D4DemoError("invalid_pdf_manifest", 500)
    normalized_pages: dict[str, int] = {}
    for evidence_id, page in pages.items():
        if (
            re.fullmatch(r"[A-Z]+-[0-9]{3}", str(evidence_id)) is None
            or type(page) is not int
            or page < 1
            or page > expected_page_count
        ):
            raise D4DemoError("invalid_pdf_manifest", 500)
        normalized_pages[str(evidence_id)] = page

    pdf_path = _project_file(project_root, pdf_path_value)
    if not pdf_path.is_file():
        raise D4DemoError("pdf_artifact_missing", 500)
    try:
        actual_bytes = pdf_path.stat().st_size
        actual_hash = _sha256_file(pdf_path)
    except OSError as exc:
        raise D4DemoError("pdf_artifact_unreadable", 500) from exc
    if actual_bytes != expected_bytes:
        raise D4DemoError("pdf_artifact_size_mismatch", 500)
    if actual_hash != expected_hash:
        raise D4DemoError("pdf_artifact_hash_mismatch", 500)

    try:
        from pypdf import PdfReader

        with pdf_path.open("rb") as handle:
            actual_page_count = len(PdfReader(handle).pages)
        parsed_pages, plumber_ids, pypdf_ids, consistent = cross_validate(pdf_path)
    except Exception as exc:
        raise D4DemoError("pdf_artifact_parse_failed", 500) from exc
    if actual_page_count != expected_page_count:
        raise D4DemoError("pdf_page_count_mismatch", 500)
    if not consistent or plumber_ids != pypdf_ids:
        raise D4DemoError("pdf_parser_evidence_mismatch", 500)
    if parsed_pages != normalized_pages:
        raise D4DemoError("rag_evidence_page_mismatch", 500)
    return pdf_path_value, normalized_pages


def _forbidden_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_").replace(" ", "_")
    return (
        "expected" in normalized
        or "gold" in normalized
        or normalized in _FORBIDDEN_KEYS
    )


def _assert_no_answer_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _forbidden_key(str(key)):
                raise D4DemoError("answer_derived_field_forbidden")
            _assert_no_answer_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_answer_fields(item)


def _validator(path: Path, code: str) -> Draft202012Validator:
    schema = _read_json(path, code)
    try:
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())
    except SchemaError as exc:
        raise D4DemoError(code, 500) from exc


def _validate(
    validator: Draft202012Validator,
    value: Any,
    code: str,
    *,
    http_status: int = 400,
) -> None:
    try:
        validator.validate(value)
    except ValidationError as exc:
        raise D4DemoError(code, http_status) from exc


def _intent_result() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "task_type": "intent",
        "status": "success",
        "data": {
            "intent": "sql",
            "needs_clarification": False,
            "required_sources": ["chinook_sql"],
            "missing_slots": [],
            "reason": "routable",
            "normalized_query": None,
        },
        "error": None,
        "warnings": [],
    }


class D4DemoService:
    """Run only the frozen public, synthetic D4-07 demonstration scenarios."""

    def __init__(
        self,
        *,
        project_root: Path | str = PROJECT_ROOT,
        scenarios_path: Path | str | None = None,
        schema_root: Path | str | None = None,
        frontend_root: Path | str | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.scenarios_path = Path(
            scenarios_path
            or self.project_root
            / "configs"
            / "demo"
            / "d4-07"
            / "public-scenarios.v1.json"
        )
        self.schema_root = Path(
            schema_root
            or self.project_root / "configs" / "schemas" / "d4-07"
        )
        self.frontend_root = Path(
            frontend_root
            or self.project_root / "src" / "frontend" / "d4-demo"
        )
        self.request_validator = _validator(
            self.schema_root / "demo-request.v1.schema.json",
            "invalid_demo_request_schema",
        )
        self.response_validator = _validator(
            self.schema_root / "demo-response.v1.schema.json",
            "invalid_demo_response_schema",
        )
        self.scenario_validator = _validator(
            self.schema_root / "public-scenarios.v1.schema.json",
            "invalid_demo_scenario_schema",
        )
        manifest = _read_json(self.scenarios_path, "invalid_demo_scenarios")
        _assert_no_answer_fields(manifest)
        _validate(
            self.scenario_validator,
            manifest,
            "invalid_demo_scenarios",
            http_status=500,
        )
        scenarios = list(manifest["scenarios"])
        ids = [str(item["id"]) for item in scenarios]
        if len(ids) != len(set(ids)):
            raise D4DemoError("duplicate_demo_scenario_id", 500)
        self.scenario_manifest = manifest
        self.scenarios = {str(item["id"]): item for item in scenarios}
        self.scenario_sha256 = canonical_text_sha256(self.scenarios_path)

        registry = DomainPackRegistry(
            pack_root=self.project_root / "domain-packs",
            schema_root=self.project_root / "configs" / "schemas" / "d4",
        )
        domain_pack = registry.load("chinook_music_store", "1.0.0")
        self.recovery = D4ClarificationRecoveryRuntime(domain_pack)
        self.source_catalog = load_source_catalog(
            self.project_root
            / "configs"
            / "routing"
            / "d4-01"
            / "source-catalog.v1.json"
        )
        self.rag_retriever, self.evidence = self._build_rag_index()

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": DEMO_VERSION,
            "mode": DEMO_MODE,
            "scenario_manifest": {
                "id": self.scenario_manifest["manifest_id"],
                "version": self.scenario_manifest["version"],
                "sha256": self.scenario_sha256,
            },
            "scenarios": [
                {
                    "scenario_id": item["id"],
                    "title": item["title"],
                    "route": item["route"],
                }
                for item in self.scenario_manifest["scenarios"]
            ],
            "boundaries": {
                "arbitrary_query_accepted": False,
                "arbitrary_sql_accepted": False,
                "online_model_enabled": False,
                "model_answer_generated": False,
                "sql_requires_d4_02_safety_gate": True,
            },
        }

    def run(self, raw_request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw_request, Mapping):
            raise D4DemoError("invalid_demo_request")
        request = dict(raw_request)
        _assert_no_answer_fields(request)
        _validate(self.request_validator, request, "invalid_demo_request")
        scenario = self.scenarios.get(str(request["scenario_id"]))
        if scenario is None:
            raise D4DemoError("unknown_demo_scenario", 404)
        try:
            response = (
                self._run_sql(request, scenario)
                if scenario["route"] == "sql"
                else self._run_rag(request, scenario)
            )
        except D4DemoError:
            raise
        except ClarificationRecoveryError as exc:
            raise D4DemoError("sql_demo_runtime_failed", 500) from exc
        except Exception as exc:
            raise D4DemoError("demo_runtime_failed", 500) from exc
        _validate(
            self.response_validator,
            response,
            "invalid_demo_response",
            http_status=500,
        )
        return response

    def _run_sql(
        self, request: Mapping[str, Any], scenario: Mapping[str, Any]
    ) -> dict[str, Any]:
        request_id = str(request["request_id"])
        state = apply_intent_result(
            create_conversation_state(
                f"session.{request_id}", str(scenario["query"])
            ),
            self.source_catalog,
            _intent_result(),
        )
        observations = build_slot_observations(scenario["initial_observations"])
        session = self.recovery.start(
            conversation_state=state,
            slot_observations=observations,
            request_id=request_id,
            recorded_at=RECORDED_AT,
        )
        for index, reply in enumerate(scenario["structured_replies"], start=1):
            question = session.pending_question
            if question is None:
                raise D4DemoError("scripted_reply_without_question", 500)
            answer = build_clarification_answer(
                {
                    "answer_id": f"reply.{request_id}.{index}",
                    "question_id": question.question_id,
                    "session_id": question.session_id,
                    "request_id": question.request_id,
                    "turn_id": question.turn_id,
                    "state_revision": question.state_revision,
                    "slot_id": question.slot_id,
                    "values": [reply["value"]],
                    "evidence_refs": [reply["evidence_ref"]],
                }
            )
            self.recovery.resume(session, answer, recorded_at=RECORDED_AT)
        proposal_data = scenario["sql_proposal"]
        proposal = SqlProposal(
            proposal_id=str(proposal_data["proposal_id"]),
            sql=str(proposal_data["sql"]),
            tables=tuple(proposal_data["tables"]),
            params=tuple(proposal_data["params"]),
        )
        execution = self.recovery.handoff_and_execute(
            session, proposal_port=_StaticProposalPort(proposal)
        )
        decision = session.current_decision
        thresholds = decision["trace_event"]["thresholds"]
        sql_result = {
            "proposal_id": proposal.proposal_id,
            "candidate_sql": proposal.sql,
            "tables": list(proposal.tables),
            "safety_approved": session.safety_approved is True,
            "execution_attempts": session.local_sqlite_execution_attempts,
            "columns": list(execution.columns) if execution is not None else [],
            "rows": [list(row) for row in execution.rows] if execution is not None else [],
            "row_count": execution.row_count if execution is not None else 0,
            "truncated": execution.truncated if execution is not None else False,
        }
        status = "success" if session.workflow_status == "executed" else "refused"
        message = (
            "只读 SQL 已通过安全门并完成本地执行。"
            if status == "success"
            else "SQL 候选未通过安全门，未执行数据库操作。"
        )
        return self._base_response(
            request=request,
            scenario=scenario,
            status=status,
            message=message,
            decision={
                "action": decision["action"],
                "reason_code": decision["reason_code"],
                "score_kind": "uncalibrated_raw_score",
                "score": decision["raw_score"],
                "calibration_version": None,
                "thresholds": {
                    "accept_min": thresholds["accept_min"],
                    "clarify_min": thresholds["clarify_min"],
                    "abstain_below": thresholds["abstain_below"],
                    "retrieval_min_score": None,
                },
                "accept_authorizes_sql_execution": False,
                "requires_d4_02_safety_gate": True,
            },
            candidates=[
                {
                    "candidate_id": item.candidate_id,
                    "display_name": item.display_name,
                    "candidate_kind": item.candidate_kind,
                    "score": item.raw_score,
                    "evidence_refs": list(item.evidence_refs),
                }
                for item in session.candidates
            ],
            sql=sql_result,
            citations=[],
            trace=[self._audit_view(item) for item in session.audit_events],
            warnings=["offline_public_demo", "uncalibrated_raw_score"],
        )

    def _run_rag(
        self, request: Mapping[str, Any], scenario: Mapping[str, Any]
    ) -> dict[str, Any]:
        ranked = self.rag_retriever.retrieve(
            str(scenario["query"]), int(scenario["top_k"])
        )
        min_score = float(scenario["min_score"])
        accepted = [(evidence_id, score) for evidence_id, score in ranked if score >= min_score]
        citations = [self._citation(evidence_id, score) for evidence_id, score in accepted]
        action = "accept" if citations else "reject"
        reason_code = "evidence_retrieved" if citations else "insufficient_evidence"
        top_score = ranked[0][1] if ranked else None
        trace = [
            {
                "sequence": 1,
                "stage": "rag_retrieval",
                "action": "retrieve",
                "reason_code": "deterministic_bm25",
                "evidence_refs": [item[0] for item in ranked],
                "artifact_sha256": _canonical_sha256(
                    [{"evidence_id": item[0], "score": round(item[1], 6)} for item in ranked]
                ),
            },
            {
                "sequence": 2,
                "stage": "evidence_gate",
                "action": action,
                "reason_code": reason_code,
                "evidence_refs": [item["evidence_id"] for item in citations],
                "artifact_sha256": _canonical_sha256(
                    {
                        "accepted_evidence_ids": [item["evidence_id"] for item in citations],
                        "min_score": min_score,
                    }
                ),
            },
        ]
        return self._base_response(
            request=request,
            scenario=scenario,
            status="success" if citations else "refused",
            message=(
                "已检索到带页码的公开证据；当前离线演示未生成模型答案。"
                if citations
                else "检索证据低于固定门槛，系统已拒绝生成答案。"
            ),
            decision={
                "action": action,
                "reason_code": reason_code,
                "score_kind": "deterministic_bm25",
                "score": top_score,
                "calibration_version": None,
                "thresholds": {
                    "accept_min": None,
                    "clarify_min": None,
                    "abstain_below": None,
                    "retrieval_min_score": min_score,
                },
                "accept_authorizes_sql_execution": False,
                "requires_d4_02_safety_gate": False,
            },
            candidates=[
                {
                    "candidate_id": evidence_id,
                    "display_name": evidence_id,
                    "candidate_kind": "rag_evidence",
                    "score": score,
                    "evidence_refs": [evidence_id],
                }
                for evidence_id, score in ranked
            ],
            sql=None,
            citations=citations,
            trace=trace,
            warnings=["offline_public_demo", "model_answer_not_generated"],
        )

    def _base_response(
        self,
        *,
        request: Mapping[str, Any],
        scenario: Mapping[str, Any],
        status: str,
        message: str,
        decision: Mapping[str, Any],
        candidates: list[dict[str, Any]],
        sql: Mapping[str, Any] | None,
        citations: list[dict[str, Any]],
        trace: list[dict[str, Any]],
        warnings: list[str],
    ) -> dict[str, Any]:
        return {
            "schema_version": DEMO_VERSION,
            "request_id": request["request_id"],
            "scenario_id": scenario["id"],
            "route": scenario["route"],
            "mode": DEMO_MODE,
            "status": status,
            "message": message,
            "decision": dict(decision),
            "candidate_evidence": candidates,
            "sql": dict(sql) if sql is not None else None,
            "citations": citations,
            "trace": trace,
            "warnings": sorted(set(warnings)),
            "disclosure": {
                "model_api_calls": 0,
                "network_evaluation_calls": 0,
                "credential_files_read": 0,
                "validation_exposures": 0,
                "hidden_test_exposures": 0,
                "model_answer_generated": False,
                "business_accuracy_proven": False,
                "generalization_proven": False,
                "innovation_proven": False,
            },
        }

    def _audit_view(self, event: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "sequence": event["sequence"],
            "stage": event["event_type"],
            "action": event["action"],
            "reason_code": event["reason_code"],
            "evidence_refs": list(event["evidence_refs"]),
            "artifact_sha256": event["artifact_sha256"],
        }

    def _citation(self, evidence_id: str, score: float) -> dict[str, Any]:
        item = self.evidence.get(evidence_id)
        if item is None:
            raise D4DemoError("rag_evidence_reference_missing", 500)
        return {
            "evidence_id": evidence_id,
            "document_id": item["document_id"],
            "document_title": item["document_title"],
            "pdf_path": item["pdf_path"],
            "page": item["page"],
            "snippet": item["text"][:240],
            "score": score,
        }

    def _build_rag_index(self) -> tuple[BM25Retriever, dict[str, dict[str, Any]]]:
        manifest_path = self.project_root / "data" / "manifests" / "pdf-v1.json"
        manifest = _read_json(manifest_path, "invalid_pdf_manifest")
        documents = manifest.get("documents")
        if not isinstance(documents, list) or not documents:
            raise D4DemoError("invalid_pdf_manifest", 500)
        evidence: dict[str, dict[str, Any]] = {}
        verified_documents = 0
        for document in documents:
            if not isinstance(document, Mapping):
                raise D4DemoError("invalid_pdf_manifest", 500)
            pdf_path, pages = _validate_pdf_document(self.project_root, document)
            verified_documents += 1
            source = self._project_file(str(document["source_markdown"]))
            try:
                lines = source.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                raise D4DemoError("invalid_rag_source", 500) from exc
            parsed_ids: set[str] = set()
            for line in lines:
                match = EVIDENCE_PATTERN.match(line.strip())
                if match is None:
                    continue
                evidence_id, text = match.groups()
                if evidence_id in evidence or evidence_id not in pages:
                    raise D4DemoError("invalid_rag_evidence_index", 500)
                parsed_ids.add(evidence_id)
                evidence[evidence_id] = {
                    "document_id": str(document["document_id"]),
                    "document_title": str(document["title"]),
                    "pdf_path": pdf_path,
                    "page": int(pages[evidence_id]),
                    "text": text,
                }
            if parsed_ids != set(pages):
                raise D4DemoError("rag_evidence_page_mismatch", 500)
        self.rag_integrity = {
            "document_count": verified_documents,
            "evidence_count": len(evidence),
            "pdf_exists_sha_bytes_page_count_verified": True,
            "dual_parser_evidence_page_verified": True,
        }
        return BM25Retriever({key: item["text"] for key, item in evidence.items()}), evidence

    def _project_file(self, relative: str) -> Path:
        return _project_file(self.project_root, relative)


class D4DemoApplication:
    """Small HTTP-independent router used by tests and the stdlib server."""

    def __init__(self, service: D4DemoService | None = None) -> None:
        self.service = service or D4DemoService()
        self.static_routes = {
            "/": (self.service.frontend_root / "index.html", "text/html; charset=utf-8"),
            "/assets/styles.css": (
                self.service.frontend_root / "styles.css",
                "text/css; charset=utf-8",
            ),
            "/assets/app.js": (
                self.service.frontend_root / "app.js",
                "text/javascript; charset=utf-8",
            ),
        }

    def handle(
        self,
        method: str,
        target: str,
        *,
        body: bytes = b"",
        content_type: str | None = None,
    ) -> HttpResponse:
        path = urlsplit(target).path
        if method == "GET" and path in self.static_routes:
            return self._static(path)
        if method == "GET" and path == "/api/v1/health":
            return self._json(
                200,
                {
                    "schema_version": DEMO_VERSION,
                    "status": "ok",
                    "mode": DEMO_MODE,
                    "model_api_calls": 0,
                },
            )
        if method == "GET" and path == "/api/v1/capabilities":
            return self._json(200, self.service.capabilities())
        if path == "/api/v1/demo":
            if method != "POST":
                return self._error("method_not_allowed", 405, extra_headers={"Allow": "POST"})
            if len(body) > MAX_REQUEST_BYTES:
                return self._error("request_too_large", 413)
            if (content_type or "").split(";", 1)[0].strip().casefold() != "application/json":
                return self._error("content_type_required", 415)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._error("invalid_json", 400)
            try:
                result = self.service.run(payload)
            except D4DemoError as exc:
                return self._error(exc.code, exc.http_status)
            return self._json(200, result)
        if path.startswith("/api/"):
            return self._error("route_not_found", 404)
        if method not in {"GET", "POST"}:
            return self._error("method_not_allowed", 405)
        return self._error("route_not_found", 404)

    def _static(self, route: str) -> HttpResponse:
        path, content_type = self.static_routes[route]
        try:
            content = path.read_bytes()
        except OSError:
            return self._error("static_asset_missing", 500)
        headers = self._security_headers()
        headers.update({"Content-Type": content_type, "Cache-Control": "no-cache"})
        return HttpResponse(200, headers, content)

    def _json(self, status: int, value: Mapping[str, Any]) -> HttpResponse:
        body = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        headers = self._security_headers()
        headers.update(
            {
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
            }
        )
        return HttpResponse(status, headers, body)

    def _error(
        self,
        code: str,
        status: int,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        response = self._json(
            status,
            {
                "schema_version": DEMO_VERSION,
                "status": "error",
                "error": {"code": code},
            },
        )
        headers = dict(response.headers)
        headers.update(extra_headers or {})
        return HttpResponse(response.status, headers, response.body)

    @staticmethod
    def _security_headers() -> dict[str, str]:
        return {
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
        }


__all__ = [
    "D4DemoApplication",
    "D4DemoError",
    "D4DemoService",
    "HttpResponse",
    "MAX_REQUEST_BYTES",
]
