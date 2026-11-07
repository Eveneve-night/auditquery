from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from src.backend.product.auth import (
    AuthApplication, AuthError, AuthService, COOKIE_NAME, SqliteAuthStore,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class D5AuthRbacTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = SqliteAuthStore(Path(self.tmp.name) / "auth.sqlite3")
        self.service = AuthService(self.store, now=self.clock, audit_key=b"a" * 32)

    def bootstrap(self, *, tls: bool = False):
        grant = self.service.create_bootstrap_grant()
        credentials = self.service.bootstrap_admin(
            grant.token,
            "admin",
            "correct horse battery staple",
            request_id="req-bootstrap",
            tls=tls,
        )
        return grant, credentials

    def assert_error(self, code: str, callback) -> AuthError:
        with self.assertRaises(AuthError) as context:
            callback()
        self.assertEqual(code, context.exception.code)
        return context.exception

    def test_bootstrap_uses_argon2id_and_persists_no_raw_credentials(self) -> None:
        grant, credentials = self.bootstrap(tls=True)
        state = self.store.read_state()
        self.assertTrue(state["password_hash"].startswith("$argon2id$"))
        persisted = self.store.persisted_values()
        self.assertNotIn(grant.token, persisted)
        self.assertNotIn(credentials.token, persisted)
        self.assertNotIn(credentials.csrf_token, persisted)
        self.assertNotIn("correct horse battery staple", persisted)
        self.assertIn("HttpOnly", credentials.cookie_header)
        self.assertIn("SameSite=Strict", credentials.cookie_header)
        self.assertIn("Secure", credentials.cookie_header)
        response = credentials.response("req-1")
        self.assertNotIn("session_token", response)
        self.assertEqual("3.0", response["api_version"])

    def test_invalid_or_expired_grant_does_not_initialize_admin(self) -> None:
        grant = self.service.create_bootstrap_grant()
        self.assert_error(
            "authentication_required",
            lambda: self.service.bootstrap_admin(
                "x" * 64, "admin", "a-valid-password", request_id="req-bad"
            ),
        )
        self.assertIsNone(self.store.read_state()["username"])
        self.clock.advance(minutes=15)
        self.assert_error(
            "authentication_required",
            lambda: self.service.bootstrap_admin(
                grant.token, "admin", "a-valid-password", request_id="req-expired"
            ),
        )
        self.assertIsNone(self.store.read_state()["username"])

    def test_concurrent_bootstrap_only_creates_one_admin(self) -> None:
        grant = self.service.create_bootstrap_grant()
        barrier = threading.Barrier(3)
        outcomes: list[str] = []

        def worker(name: str) -> None:
            barrier.wait()
            try:
                self.service.bootstrap_admin(
                    grant.token, name, "a-valid-password", request_id=f"req-{name}"
                )
                outcomes.append("ok")
            except AuthError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("one", "two")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(1, outcomes.count("ok"))
        self.assertEqual(1, outcomes.count("admin_already_initialized"))

    def test_login_is_content_safe_rotates_session_and_survives_restart(self) -> None:
        _, first = self.bootstrap()
        self.assert_error(
            "authentication_required",
            lambda: self.service.login(
                "missing", "wrong-password", source="127.0.0.1", request_id="req-bad"
            ),
        )
        second = self.service.login(
            "ADMIN",
            "correct horse battery staple",
            source="127.0.0.1",
            request_id="req-login",
        )
        self.assertNotEqual(first.token, second.token)
        restarted = AuthService(self.store, now=self.clock, audit_key=b"a" * 32)
        principal = restarted.authorize_admin(second.token, request_id="req-read")
        self.assertEqual("admin", principal.username)

    def test_account_and_source_limit_lock_then_recover(self) -> None:
        self.bootstrap()
        for index in range(5):
            self.assert_error(
                "authentication_required",
                lambda index=index: self.service.login(
                    "admin",
                    f"wrong-{index}",
                    source="198.51.100.10",
                    request_id=f"req-{index}",
                ),
            )
        self.assert_error(
            "rate_limited",
            lambda: self.service.login(
                "admin",
                "correct horse battery staple",
                source="198.51.100.10",
                request_id="req-blocked-account",
            ),
        )
        self.assert_error(
            "rate_limited",
            lambda: self.service.login(
                "different",
                "wrong-password",
                source="198.51.100.10",
                request_id="req-blocked-source",
            ),
        )
        self.clock.advance(minutes=15)
        credentials = self.service.login(
            "admin",
            "correct horse battery staple",
            source="198.51.100.10",
            request_id="req-recovered",
        )
        self.assertTrue(credentials.token)

    def test_csrf_failure_changes_no_session_state(self) -> None:
        _, credentials = self.bootstrap()
        before = self._session_row(credentials.token)
        self.clock.advance(seconds=30)
        self.assert_error(
            "csrf_invalid",
            lambda: self.service.guard_admin_path(
                "POST",
                "/api/v3/admin/documents",
                credentials.token,
                csrf_token="wrong",
                request_id="req-csrf",
            ),
        )
        after = self._session_row(credentials.token)
        self.assertEqual(before["last_seen_at"], after["last_seen_at"])
        self.assertIsNone(after["revoked_at"])
        self.assertEqual("csrf_rejected", self.store.audit_events()[-1]["event_type"])

    def test_errors_expose_frozen_safe_transport_metadata(self) -> None:
        expected = {
            "authentication_required": (401, "需要有效身份。", False),
            "session_invalid": (401, "会话无效或已过期。", False),
            "csrf_invalid": (403, "请求安全校验失败。", False),
            "rate_limited": (429, "请求过于频繁，请稍后重试。", True),
        }
        for code, values in expected.items():
            error = AuthError(code)
            self.assertEqual(values, (error.http_status, error.safe_message, error.retryable))

    def test_unified_guard_protects_reads_and_requires_csrf_for_writes(self) -> None:
        _, credentials = self.bootstrap()
        self.assert_error(
            "authentication_required",
            lambda: self.service.guard_admin_path(
                "GET", "/api/v3/admin/health", None, request_id="req-none"
            ),
        )
        principal = self.service.guard_admin_path(
            "GET", "/api/v3/admin/health", credentials.token, request_id="req-health"
        )
        self.assertEqual("admin", principal.username)
        self.service.guard_admin_path(
            "DELETE",
            "/api/v3/admin/documents/doc-1",
            credentials.token,
            csrf_token=credentials.csrf_token,
            request_id="req-delete",
        )
        with self.assertRaises(ValueError):
            self.service.guard_admin_path(
                "GET", "/api/v3/sessions", credentials.token, request_id="req-wrong-path"
            )

    def test_idle_absolute_logout_and_revoke_all_invalidate_tokens(self) -> None:
        _, first = self.bootstrap()
        second = self.service.login(
            "admin",
            "correct horse battery staple",
            source="127.0.0.2",
            request_id="req-login",
        )
        self.assertEqual(1, self.service.logout(first.token, first.csrf_token, request_id="req-out"))
        self.assert_error(
            "session_invalid",
            lambda: self.service.authorize_admin(first.token, request_id="req-replay"),
        )
        third = self.service.login(
            "admin",
            "correct horse battery staple",
            source="127.0.0.3",
            request_id="req-login-2",
        )
        self.assertEqual(2, self.service.revoke_all(second.token, second.csrf_token, request_id="req-all"))
        self.assert_error(
            "session_invalid",
            lambda: self.service.authorize_admin(third.token, request_id="req-old-generation"),
        )

        fresh = self.service.login(
            "admin",
            "correct horse battery staple",
            source="127.0.0.4",
            request_id="req-login-3",
        )
        self.clock.advance(minutes=30)
        self.assert_error(
            "session_invalid",
            lambda: self.service.authorize_admin(fresh.token, request_id="req-idle"),
        )

    def test_audit_is_whitelisted_and_contains_no_raw_input(self) -> None:
        _, credentials = self.bootstrap()
        self.service.logout(credentials.token, credentials.csrf_token, request_id="req-out")
        events = self.store.audit_events()
        self.assertEqual(["admin_bootstrap", "admin_logout"], [e["event_type"] for e in events])
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn(credentials.token, serialized)
        self.assertNotIn(credentials.csrf_token, serialized)
        expected = {
            "event_id", "event_type", "occurred_at", "request_id", "actor_role",
            "subject_type", "subject_id_hash", "outcome", "error_code",
            "source_fingerprint", "resource_revision", "usage_counts",
        }
        self.assertTrue(all(set(event) == expected for event in events))

    def test_password_change_is_atomic_and_revokes_all_sessions(self) -> None:
        _, credentials = self.bootstrap()
        before = self.store.read_state()
        self.assert_error(
            "authentication_required",
            lambda: self.service.change_password(
                credentials.token,
                credentials.csrf_token,
                "wrong-current-password",
                "a-new-valid-password",
                request_id="req-change-bad",
            ),
        )
        self.assertEqual(before["session_generation"], self.store.read_state()["session_generation"])
        generation = self.service.change_password(
            credentials.token,
            credentials.csrf_token,
            "correct horse battery staple",
            "a-new-valid-password",
            request_id="req-change",
        )
        self.assertEqual(1, generation)
        self.assert_error(
            "session_invalid",
            lambda: self.service.authorize_admin(credentials.token, request_id="req-old"),
        )
        logged_in = self.service.login(
            "admin",
            "a-new-valid-password",
            source="127.0.0.5",
            request_id="req-new-login",
        )
        self.assertTrue(logged_in.token)

    def test_default_fingerprints_are_stable_across_restart(self) -> None:
        service = AuthService(self.store, now=self.clock)
        first = service._fingerprint("source", "127.0.0.1")
        restarted = AuthService(self.store, now=self.clock)
        self.assertEqual(first, restarted._fingerprint("source", "127.0.0.1"))

    def test_api_adapter_bootstrap_login_logout_and_admin_non_disclosure(self) -> None:
        app = AuthApplication(self.service)
        grant = self.service.create_bootstrap_grant()
        bootstrap = app.handle(
            "POST", "/api/v3/auth/admin/bootstrap", request_id="req-api-bootstrap",
            tls=True,
            body=json.dumps({
                "api_version": "3.0", "contract_version": "1.0.0",
                "setup_token": grant.token, "username": "admin",
                "password": "correct horse battery staple",
            }).encode(),
        )
        self.assertEqual(200, bootstrap.status)
        self.assertIn("HttpOnly", bootstrap.headers["Set-Cookie"])
        self.assertIn("Secure", bootstrap.headers["Set-Cookie"])
        payload = json.loads(bootstrap.body)
        token = bootstrap.headers["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]

        hidden = app.handle(
            "GET", "/api/v3/admin/documents/missing", request_id="req-hidden"
        )
        self.assertEqual(401, hidden.status)
        self.assertEqual("authentication_required", json.loads(hidden.body)["error"]["code"])
        authenticated = app.handle(
            "GET", "/api/v3/admin/documents/missing", request_id="req-known",
            cookies={COOKIE_NAME: token},
        )
        self.assertEqual(404, authenticated.status)

        logout = app.handle(
            "POST", "/api/v3/admin/logout", request_id="req-api-logout",
            body=b"{}", cookies={COOKIE_NAME: token},
            headers={"X-CSRF-Token": payload["csrf_token"]}, tls=True,
        )
        self.assertEqual(200, logout.status)
        self.assertIn("Max-Age=0", logout.headers["Set-Cookie"])

    def test_audit_hashes_all_caller_controlled_identifiers(self) -> None:
        canary = "sk-testonly-audit-value-must-not-persist"
        grant = self.service.create_bootstrap_grant()
        credentials = self.service.bootstrap_admin(
            grant.token,
            "admin",
            "correct horse battery staple",
            request_id=canary,
            source=canary,
        )
        self.assert_error(
            "csrf_invalid",
            lambda: self.service.guard_admin_path(
                "POST",
                "/api/v3/admin/documents",
                credentials.token,
                csrf_token=canary,
                request_id=canary,
            ),
        )
        self.assert_error(
            "session_invalid",
            lambda: self.service.authorize_admin(canary, request_id=canary),
        )
        serialized = json.dumps(self.store.audit_events(), ensure_ascii=False)
        self.assertNotIn(canary, serialized)
        self.assertIn(
            "authorization_denied",
            [event["event_type"] for event in self.store.audit_events()],
        )
        self.assertTrue(
            all(
                event["request_id"].startswith("req_")
                and len(event["request_id"]) == 68
                for event in self.store.audit_events()
            )
        )

    def test_bootstrap_has_independent_limit_and_recovers_without_side_effect(self) -> None:
        grant = self.service.create_bootstrap_grant()
        for index in range(5):
            self.assert_error(
                "authentication_required",
                lambda index=index: self.service.bootstrap_admin(
                    f"{'x' * 63}{index}",
                    "admin",
                    "a-valid-password",
                    request_id=f"req-bootstrap-fail-{index}",
                    source="198.51.100.20",
                ),
            )
        self.assert_error(
            "rate_limited",
            lambda: self.service.bootstrap_admin(
                grant.token,
                "admin",
                "a-valid-password",
                request_id="req-bootstrap-blocked",
                source="198.51.100.20",
            ),
        )
        self.assertIsNone(self.store.read_state()["username"])
        with self.store._connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0])
        self.clock.advance(minutes=15)
        recovered_grant = self.service.create_bootstrap_grant()
        credentials = self.service.bootstrap_admin(
            recovered_grant.token,
            "admin",
            "a-valid-password",
            request_id="req-bootstrap-recovered",
            source="198.51.100.20",
        )
        self.assertTrue(credentials.token)

    def test_logout_routes_use_unified_missing_identity_semantics_and_audit(self) -> None:
        app = AuthApplication(self.service)
        for path in (
            "/api/v3/admin/logout",
            "/api/v3/admin/sessions/revoke-all",
        ):
            response = app.handle("POST", path, request_id="req-missing", body=b"{}")
            self.assertEqual(401, response.status)
            self.assertEqual(
                "authentication_required", json.loads(response.body)["error"]["code"]
            )
        invalid = app.handle(
            "GET",
            "/api/v3/admin/health",
            request_id="req-invalid-session",
            cookies={COOKIE_NAME: "not-a-session-token"},
        )
        self.assertEqual(401, invalid.status)
        self.assertEqual(
            "authentication_required", json.loads(invalid.body)["error"]["code"]
        )
        events = self.store.audit_events()
        self.assertEqual(
            [
                "authorization_denied",
                "authorization_denied",
                "authorization_denied",
            ],
            [event["event_type"] for event in events],
        )
        self.assertTrue(all(event["outcome"] == "rejected" for event in events))

    def test_storage_failure_is_safe_and_rolls_back_partial_bootstrap(self) -> None:
        app = AuthApplication(self.service)
        grant = self.service.create_bootstrap_grant()
        original = self.service._append_audit

        def fail_audit(*args, **kwargs):
            raise sqlite3.OperationalError("raw storage failure must not escape")

        self.service._append_audit = fail_audit
        response = app.handle(
            "POST",
            "/api/v3/auth/admin/bootstrap",
            request_id="req-storage-failure",
            source="198.51.100.30",
            body=json.dumps(
                {
                    "api_version": "3.0",
                    "contract_version": "1.0.0",
                    "setup_token": grant.token,
                    "username": "admin",
                    "password": "a-valid-password",
                }
            ).encode(),
        )
        self.assertEqual(503, response.status)
        self.assertEqual("service_unavailable", json.loads(response.body)["error"]["code"])
        self.assertNotIn(b"raw storage failure", response.body)
        self.assertIsNone(self.store.read_state()["username"])
        with self.store._connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0])
        self.service._append_audit = original
        credentials = self.service.bootstrap_admin(
            grant.token,
            "admin",
            "a-valid-password",
            request_id="req-storage-retry",
            source="198.51.100.30",
        )
        self.assertTrue(credentials.token)

    def test_corrupt_store_maps_to_stable_service_unavailable(self) -> None:
        _, credentials = self.bootstrap()
        app = AuthApplication(self.service)
        Path(self.store.path).write_bytes(b"not-a-sqlite-database")
        response = app.handle(
            "GET",
            "/api/v3/admin/health",
            request_id="req-corrupt-store",
            cookies={COOKIE_NAME: credentials.token},
        )
        self.assertEqual(503, response.status)
        payload = json.loads(response.body)
        self.assertEqual("service_unavailable", payload["error"]["code"])
        self.assertNotIn("sqlite", response.body.decode().casefold())

    def _session_row(self, token: str):
        import hashlib

        with self.store._connect() as connection:
            return connection.execute(
                "SELECT * FROM admin_sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()


if __name__ == "__main__":
    unittest.main()
