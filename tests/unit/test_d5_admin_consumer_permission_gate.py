"""B-side permission handoff checks for future D5-03/04 admin consumers.

These checks exercise the shared auth boundary, not C's future document or UI
implementation. The frozen endpoint catalog drives the matrix so new admin
routes cannot silently skip the B-side handoff checks.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from src.backend.product.auth import (
    AuthApplication,
    AuthError,
    AuthService,
    COOKIE_NAME,
    SqliteAuthStore,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "configs" / "contracts" / "d5" / "product-layer.v1.json"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class D5AdminConsumerPermissionGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        store = SqliteAuthStore(Path(self.temp_dir.name) / "auth.sqlite3")
        self.service = AuthService(
            store,
            now=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc),
            audit_key=b"b-side-test-only-audit-key-000000",
        )
        grant = self.service.create_bootstrap_grant()
        self.admin = self.service.bootstrap_admin(
            grant.token,
            "admin",
            "test-only-password-for-admin",
            request_id="req-bootstrap",
        )
        self.other_session = self.service.login(
            "admin",
            "test-only-password-for-admin",
            source="test-other-session",
            request_id="req-login",
        )
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.endpoints = [
            endpoint for endpoint in contract["endpoints"]
            if endpoint["path"].startswith("/api/v3/admin/")
        ]

    @staticmethod
    def _path(endpoint: dict) -> str:
        return endpoint["path"].replace("{document_id}", "doc-test-only")

    def test_frozen_admin_routes_all_require_server_session_and_write_csrf(self) -> None:
        self.assertTrue(self.endpoints)
        self.assertTrue(any("/documents" in item["path"] for item in self.endpoints))
        self.assertTrue(any("/secrets/" in item["path"] for item in self.endpoints))
        for endpoint in self.endpoints:
            with self.subTest(endpoint=endpoint["id"]):
                self.assertEqual("admin_session", endpoint["auth"])
                self.assertEqual(["admin"], endpoint["roles"])
                expected_csrf = (
                    "required" if endpoint["method"] in WRITE_METHODS else "not_applicable"
                )
                self.assertEqual(expected_csrf, endpoint["csrf"])
                method, path = endpoint["method"], self._path(endpoint)
                with self.assertRaises(AuthError) as missing:
                    self.service.guard_admin_path(
                        method, path, None, request_id="req-missing"
                    )
                self.assertEqual("authentication_required", missing.exception.code)
                if method in WRITE_METHODS:
                    for csrf in (None, "wrong-test-token", self.other_session.csrf_token):
                        with self.assertRaises(AuthError) as rejected:
                            self.service.guard_admin_path(
                                method,
                                path,
                                self.admin.token,
                                csrf_token=csrf,
                                request_id="req-csrf",
                            )
                        self.assertEqual("csrf_invalid", rejected.exception.code)
                principal = self.service.guard_admin_path(
                    method,
                    path,
                    self.admin.token,
                    csrf_token=self.admin.csrf_token,
                    request_id="req-valid",
                )
                self.assertEqual("admin", principal.username)

    def test_public_auth_adapter_rejects_document_routes_before_business_dispatch(self) -> None:
        app = AuthApplication(self.service)
        document_endpoints = [
            item for item in self.endpoints if "/documents" in item["path"]
        ]
        self.assertTrue(document_endpoints)
        for endpoint in document_endpoints:
            with self.subTest(endpoint=endpoint["id"]):
                method, path = endpoint["method"], self._path(endpoint)
                anonymous = app.handle(method, path, request_id="req-anonymous")
                self.assertEqual(401, anonymous.status)
                self.assertEqual(
                    "authentication_required",
                    json.loads(anonymous.body)["error"]["code"],
                )
                if method in WRITE_METHODS:
                    no_csrf = app.handle(
                        method,
                        path,
                        request_id="req-no-csrf",
                        cookies={COOKIE_NAME: self.admin.token},
                    )
                    self.assertEqual(403, no_csrf.status)
                    self.assertEqual(
                        "csrf_invalid", json.loads(no_csrf.body)["error"]["code"]
                    )


if __name__ == "__main__":
    unittest.main()
