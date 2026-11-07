"""Narrow D5 visitor-to-D4 SQL adapter; the D4 safety gate remains authoritative."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, nullcontext
from pathlib import Path
import secrets
import threading
from typing import Any, Mapping

from ..agent.loop_guard import LoopPolicy
from ..api.online_sql_service import OnlineSqlService
from ..llm import BailianConfig, BailianProvider, LLMGateway, UsageLedger
from ..llm.credentials import Credential, CredentialError
from .secret_store import SecretStoreError, SqliteSecretStore


ROOT = Path(__file__).resolve().parents[3]


class SecretStoreCredentialProvider:
    """Read the configured key only at the explicit provider-call boundary."""

    def __init__(self, store: SqliteSecretStore) -> None:
        self.store = store

    def load(self) -> Credential:
        try:
            return Credential(self.store.get_for_provider_call(), "secret_store")
        except SecretStoreError:
            raise CredentialError("SecretStore credential unavailable") from None


def build_approved_sql_adapter(store: SqliteSecretStore) -> "VisitorSqlAdapter":
    """Construct the configured pipeline; no credential read or API call here."""
    from .model_settings import ModelSettings, ConfiguredProvider
    ledger = UsageLedger()
    provider = ConfiguredProvider(ModelSettings(store), ledger)
    gateway = LLMGateway(provider, LoopPolicy.from_file(ROOT / "configs" / "policies" /
                                                        "agent-loop-limits.v1.json"), usage_ledger=ledger)
    return VisitorSqlAdapter(
        OnlineSqlService(gateway, project_root=ROOT, table_name=None, infer_count_slots=True),
        usage_ledger=ledger,
    )


class VisitorSqlAdapter:
    """Translate public guest revisions to one D4 SQL workflow per question.

    This adapter never constructs SQL itself.  The supplied OnlineSqlService
    retains proposal validation, the D4-02 read-only gate, and execution limits.
    A process restart drops only the in-flight clarification binding; no old
    answer can resume without a fresh, validated workflow.
    """

    def __init__(self, service: OnlineSqlService, *, usage_ledger: UsageLedger | None = None) -> None:
        self.service = service
        self.usage_ledger = usage_ledger
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self, request: Mapping[str, Any]):
        """Stage D4 session mutations until D5 validates and commits its turn.

        A model call cannot be undone; usage telemetry is deliberately retained.
        Only authoritative clarification/replay state is restored on failure.
        """
        sid = str(request["session_id"])
        service_lock = getattr(self.service, "_lock", None)
        guard = getattr(self.service, "guard", None)
        guard_lock = getattr(guard, "_lock", None)
        with self._lock, (service_lock or nullcontext()), (guard_lock or nullcontext()):
            old_pending = deepcopy(self._pending.get(sid))
            service_sessions = getattr(self.service, "_sessions", None)
            guard_sessions = getattr(guard, "_sessions", None)
            if isinstance(service_sessions, dict) and isinstance(guard_sessions, dict):
                old_keys = set(service_sessions)
                old_guard_keys = set(guard_sessions)
                internal_sid = (old_pending or {}).get("internal_sid")
                old_record, old_binding = deepcopy((service_sessions.get(internal_sid),
                                                    guard_sessions.get(internal_sid)))
                old_answers = set(guard._consumed_answer_ids)
                old_questions = set(guard._consumed_question_ids)
            else:
                old_keys = old_guard_keys = None
                internal_sid = old_record = old_binding = old_answers = old_questions = None
            committed = False

            def accept() -> None:
                nonlocal committed
                committed = True

            try:
                yield self.submit(request), accept
            finally:
                if not committed:
                    if old_pending is None:
                        self._pending.pop(sid, None)
                    else:
                        self._pending[sid] = old_pending
                    if old_keys is not None and old_guard_keys is not None:
                        for key in set(service_sessions) - old_keys:
                            service_sessions.pop(key, None)
                        for key in set(guard_sessions) - old_guard_keys:
                            guard_sessions.pop(key, None)
                        if internal_sid is not None:
                            if old_record is not None:
                                service_sessions[internal_sid] = old_record
                            if old_binding is not None:
                                guard_sessions[internal_sid] = old_binding
                        guard._consumed_answer_ids = old_answers
                        guard._consumed_question_ids = old_questions

    def submit(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        external_sid = str(request["session_id"])
        with self._lock:
            if "question" in request:
                internal_sid = "sql." + secrets.token_urlsafe(18).replace("-", "_")
                internal_request = {
                    "schema_version": "2.0.0", "operation": "submit_query",
                    "session_id": internal_sid, "request_id": request["request_id"],
                    "turn_id": 0, "state_revision": 0, "query": request["question"],
                }
            else:
                binding = self._pending.get(external_sid)
                if binding is None:
                    raise ValueError("no_pending_clarification")
                pending = binding["pending"]
                for field in ("request_id", "question_id", "slot_id"):
                    if request[field] != pending[field]:
                        raise ValueError("clarification_binding_mismatch")
                internal_sid = binding["internal_sid"]
                internal_request = {
                    "schema_version": "2.0.0", "operation": "submit_clarification",
                    "answer_id": request["answer_id"], "session_id": internal_sid,
                    "request_id": pending["request_id"], "turn_id": pending["turn_id"],
                    "state_revision": pending["state_revision"],
                    "question_id": pending["question_id"], "slot_id": pending["slot_id"],
                    "values": request["values"], "evidence_refs": request["evidence_refs"],
                }
            result = deepcopy(self.service.submit(internal_request))
            pending = result.get("pending_question")
            if pending is None:
                self._pending.pop(external_sid, None)
            else:
                self._pending[external_sid] = {"internal_sid": internal_sid, "pending": deepcopy(pending)}
                pending["session_id"] = external_sid
                pending["turn_id"] = request["turn_id"] + 1
                pending["state_revision"] = request["state_revision"] + 1
            result["session_id"] = external_sid
            result["turn_id"] = request["turn_id"] + 1
            result["state_revision"] = request["state_revision"] + 1
            return result


__all__ = ["SecretStoreCredentialProvider", "VisitorSqlAdapter", "build_approved_sql_adapter"]
