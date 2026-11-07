"""Offline, content-safe state probe for an isolated D5-05 Docker volume."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from backend.product.document_store import DocumentAuditContext, DocumentService
from backend.product.server import ProductRuntime


FAKE_API_KEY = "sk-testonly-d5-sandbox-key-0001"
REPLACEMENT_PASSWORD = "testonly-new-password-123"


def seed(instance: Path, root: Path, *, master_key: bytes | None = None) -> None:
    runtime = ProductRuntime(instance, master_key=master_key, bind_host="0.0.0.0")
    if runtime.ready() or not runtime.auth.default_password_active():
        raise AssertionError("fresh_nonloopback_instance_must_fail_closed")

    contract = json.loads(
        (root / "configs/contracts/d5/admin-bootstrap.v2.json").read_text(encoding="utf-8")
    )
    initial_password = contract["initial_credentials"]["initial_password"]
    session = runtime.auth.login("admin", initial_password, source="sandbox", request_id="req.sandbox.login")
    runtime.auth.change_password(
        session.token, session.csrf_token, initial_password, REPLACEMENT_PASSWORD,
        request_id="req.sandbox.password",
    )

    pdf = (root / "data/raw/pdf/product_manual.pdf").read_bytes()
    documents = DocumentService(runtime.documents)
    audit = DocumentAuditContext("req.sandbox.document", "sandbox", "sandbox")
    uploaded = documents.upload(
        filename="product_manual.pdf", content_type="application/pdf", declared_bytes=len(pdf),
        content_sha256=hashlib.sha256(pdf).hexdigest(), file_bytes=pdf, audit=audit,
    )
    if documents.publish(uploaded.document_id, audit).state != "published":
        raise AssertionError("document_not_published")
    runtime.secret_store.put_if_absent(FAKE_API_KEY)
    verify(instance, root, master_key=master_key)


def verify(instance: Path, root: Path, *, master_key: bytes | None = None) -> None:
    runtime = ProductRuntime(instance, master_key=master_key, bind_host="0.0.0.0")
    if not runtime.ready() or runtime.auth.default_password_active():
        raise AssertionError("readiness_or_password_state_lost")
    records = runtime.documents.list_records()
    pdf = (root / "data/raw/pdf/product_manual.pdf").read_bytes()
    if (len(records) != 1 or records[0].state != "published" or
            records[0].sha256 != hashlib.sha256(pdf).hexdigest() or
            runtime.documents.get_active_index() != records[0].index_version):
        raise AssertionError("published_document_or_index_lost")
    if (not runtime.secret_store.status().configured or
            runtime.secret_store.get_for_provider_call() != FAKE_API_KEY):
        raise AssertionError("fake_secret_state_lost")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"seed", "verify"}:
        raise SystemExit("usage: d5_05_container_probe.py seed|verify")
    globals()[sys.argv[1]](Path("/instance"), Path("/app"))
    print(json.dumps({"status": "pass", "phase": sys.argv[1], "mode": "offline"}))
