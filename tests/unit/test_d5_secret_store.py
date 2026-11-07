from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from src.backend.product.auth import AuthService, SqliteAuthStore
from src.backend.product.secret_store import (
    MASTER_KEY_ENV,
    MODEL,
    PROVIDER,
    SecretApplication,
    SecretAudit,
    SecretService,
    SecretStoreError,
    SqliteSecretStore,
    load_master_key,
)


FAKE_KEY_ONE = "sk-testonly-secret-AAAA1111"
FAKE_KEY_TWO = "sk-testonly-secret-BBBB2222"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 9, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


class FailingSecretStore(SqliteSecretStore):
    def _after_mutation(self, _connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("raw failure with " + FAKE_KEY_TWO)


class D5SecretStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.secret_path = root / "secrets.sqlite3"
        self.auth_path = root / "auth.sqlite3"
        self.master_key = b"M" * 32
        self.rate_key = b"R" * 32
        self.store = SqliteSecretStore(self.secret_path, master_key=self.master_key)
        self.service = SecretService(self.store, rate_key=self.rate_key)
        self.clock = Clock()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_error(self, code: str, operation) -> SecretStoreError:
        with self.assertRaises(SecretStoreError) as context:
            operation()
        self.assertEqual(code, context.exception.code)
        self.assertNotIn(FAKE_KEY_ONE, str(context.exception))
        self.assertNotIn(FAKE_KEY_TWO, str(context.exception))
        return context.exception

    def test_save_is_authenticated_encrypted_and_response_is_content_safe(self) -> None:
        status = self.store.put_if_absent(FAKE_KEY_ONE, now=self.clock())
        self.assertTrue(status.configured)
        self.assertEqual("1111", status.last_four)
        self.assertEqual(FAKE_KEY_ONE, self.store.get_for_provider_call())
        self.assertNotIn(FAKE_KEY_ONE.encode(), self.secret_path.read_bytes())
        self.assertNotIn(self.master_key, self.secret_path.read_bytes())
        payload = status.response("req-secret-status")
        self.assertEqual(
            {
                "api_version",
                "contract_version",
                "request_id",
                "configured",
                "provider",
                "model",
                "source",
                "updated_at",
                "last_four",
            },
            set(payload),
        )
        self.assertNotIn(FAKE_KEY_ONE, json.dumps(payload))
        schema = json.loads(Path("configs/schemas/d5/api-v3.schema.json").read_text("utf-8"))
        validation_schema = {
            "$schema": schema["$schema"],
            "$ref": "#/$defs/key_status_response",
            "$defs": schema["$defs"],
        }
        Draft202012Validator(
            validation_schema, format_checker=FormatChecker()
        ).validate(payload)

    def test_restart_recovers_only_with_correct_external_master_key(self) -> None:
        self.store.put_if_absent(FAKE_KEY_ONE)
        recovered = SqliteSecretStore(self.secret_path, master_key=self.master_key)
        self.assertTrue(recovered.startup_check().configured)
        self.assertEqual(FAKE_KEY_ONE, recovered.get_for_provider_call())
        missing = SqliteSecretStore(self.secret_path, master_key=None)
        self.assert_error("service_unavailable", missing.status)
        self.assert_error("service_unavailable", missing.startup_check)
        wrong = SqliteSecretStore(self.secret_path, master_key=b"W" * 32)
        self.assert_error("service_unavailable", wrong.startup_check)

    def test_tampered_ciphertext_fails_closed_without_plaintext(self) -> None:
        self.store.put_if_absent(FAKE_KEY_ONE)
        with self.store._connect() as connection:
            blob = bytearray(
                connection.execute(
                    "SELECT ciphertext FROM provider_secrets WHERE provider=?", (PROVIDER,)
                ).fetchone()[0]
            )
            blob[-1] ^= 1
            connection.execute(
                "UPDATE provider_secrets SET ciphertext=? WHERE provider=?",
                (bytes(blob), PROVIDER),
            )
        self.assert_error("service_unavailable", self.store.get_for_provider_call)

    def test_tampered_status_metadata_is_authenticated(self) -> None:
        self.store.put_if_absent(FAKE_KEY_ONE)
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE provider_secrets SET last_four='9999' WHERE provider=?",
                (PROVIDER,),
            )
        self.assert_error("service_unavailable", self.store.status)
        self.assert_error("service_unavailable", self.store.get_for_provider_call)

    def test_replace_delete_and_recreate_use_monotonic_revisions(self) -> None:
        first = self.store.put_if_absent(FAKE_KEY_ONE)
        self.assert_error(
            "state_conflict",
            lambda: self.store.replace(FAKE_KEY_TWO, expected_revision=0),
        )
        second = self.store.replace(FAKE_KEY_TWO, expected_revision=first.revision)
        self.assertEqual(first.revision + 1, second.revision)
        self.assertEqual(FAKE_KEY_TWO, self.store.get_for_provider_call())
        self.assert_error(
            "state_conflict", lambda: self.store.delete(expected_revision=first.revision)
        )
        deleted = self.store.delete(expected_revision=second.revision)
        self.assertFalse(deleted.configured)
        self.assert_error("secret_not_configured", self.store.get_for_provider_call)
        recreated = self.store.put_if_absent(FAKE_KEY_ONE)
        self.assertEqual(deleted.revision + 1, recreated.revision)

    def test_failed_replace_rolls_back_and_preserves_old_ciphertext(self) -> None:
        original = self.store.put_if_absent(FAKE_KEY_ONE)
        failing = FailingSecretStore(self.secret_path, master_key=self.master_key)
        with self.assertRaises(sqlite3.OperationalError):
            failing.replace(FAKE_KEY_TWO, expected_revision=original.revision)
        self.assertEqual(original, self.store.status())
        self.assertEqual(FAKE_KEY_ONE, self.store.get_for_provider_call())
        serialized = self.secret_path.read_bytes()
        self.assertNotIn(FAKE_KEY_ONE.encode(), serialized)
        self.assertNotIn(FAKE_KEY_TWO.encode(), serialized)

    def test_concurrent_replace_allows_exactly_one_revision_winner(self) -> None:
        first = self.store.put_if_absent(FAKE_KEY_ONE)
        barrier = threading.Barrier(2)
        results: list[str] = []

        def replace(value: str) -> None:
            barrier.wait()
            try:
                self.store.replace(value, expected_revision=first.revision)
                results.append("ok")
            except SecretStoreError as exc:
                results.append(exc.code)

        threads = [
            threading.Thread(target=replace, args=(FAKE_KEY_ONE,)),
            threading.Thread(target=replace, args=(FAKE_KEY_TWO,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(["ok", "state_conflict"], results)
        self.assertEqual(first.revision + 1, self.store.status().revision)

    def test_master_key_loader_uses_only_one_explicit_source(self) -> None:
        encoded = base64.urlsafe_b64encode(self.master_key).decode("ascii")
        self.assertEqual(
            self.master_key, load_master_key(environ={MASTER_KEY_ENV: encoded})
        )
        secret_file = Path(self.temp.name) / "docker-secret"
        secret_file.write_text(encoded, encoding="ascii")
        self.assertEqual(
            self.master_key,
            load_master_key(environ={}, docker_secret_path=secret_file),
        )
        self.assert_error("service_unavailable", lambda: load_master_key(environ={}))
        self.assert_error(
            "service_unavailable",
            lambda: load_master_key(
                environ={MASTER_KEY_ENV: encoded}, docker_secret_path=secret_file
            ),
        )
        self.assert_error(
            "service_unavailable",
            lambda: load_master_key(environ={MASTER_KEY_ENV: "not-base64"}),
        )

    def test_invalid_key_formats_fail_before_storage(self) -> None:
        for value in (
            "short",
            "sk-testonly-spaces are forbidden-1111",
            "sk-testonly-secret-AAAA!!!!",
            "x" * 513,
        ):
            with self.subTest(value=value[:20]):
                self.assert_error("invalid_request", lambda value=value: self.service.save(value))
        self.assertFalse(self.store.status().configured)

    def test_save_replace_delete_never_invoke_connectivity_probe(self) -> None:
        calls: list[str] = []

        def probe(key: str):
            calls.append(key)
            return {"requests": 1}

        self.service.save(FAKE_KEY_ONE)
        self.service.replace(FAKE_KEY_TWO)
        self.service.delete()
        self.assertEqual([], calls)
        self.assertIsNotNone(probe)

    def test_connectivity_requires_confirmation_and_returns_only_counts(self) -> None:
        self.service.save(FAKE_KEY_ONE)
        seen: list[str] = []

        def probe(key: str):
            seen.append(key)
            return {"requests": 1, "input_tokens": 2, "output_tokens": 0}

        self.assert_error(
            "invalid_request",
            lambda: self.service.test_connectivity(
                explicit_confirmation=False, source="198.51.100.2", probe=probe
            ),
        )
        summary = self.service.test_connectivity(
            explicit_confirmation=True, source="198.51.100.2", probe=probe
        )
        self.assertEqual([FAKE_KEY_ONE], seen)
        self.assertEqual(PROVIDER, summary["provider"])
        self.assertEqual(MODEL, summary["model"])
        self.assertNotIn(FAKE_KEY_ONE, json.dumps(summary))

    def test_connectivity_has_independent_persistent_limit(self) -> None:
        self.service.save(FAKE_KEY_ONE)
        calls: list[int] = []

        def probe(_key: str):
            calls.append(1)
            return {"requests": 1}

        for _ in range(3):
            self.service.test_connectivity(
                explicit_confirmation=True, source="198.51.100.3", probe=probe
            )
        self.assert_error(
            "rate_limited",
            lambda: self.service.test_connectivity(
                explicit_confirmation=True, source="198.51.100.3", probe=probe
            ),
        )
        self.assertEqual(3, len(calls))
        restarted = SecretService(
            SqliteSecretStore(self.secret_path, master_key=self.master_key),
            rate_key=self.rate_key,
        )
        self.assert_error(
            "rate_limited",
            lambda: restarted.test_connectivity(
                explicit_confirmation=True, source="198.51.100.3", probe=probe
            ),
        )

    def test_probe_failure_and_invalid_usage_are_content_safe(self) -> None:
        self.service.save(FAKE_KEY_ONE)

        def fail(key: str):
            raise RuntimeError("provider leaked " + key)

        self.assert_error(
            "service_unavailable",
            lambda: self.service.test_connectivity(
                explicit_confirmation=True, source="one", probe=fail
            ),
        )
        self.assert_error(
            "service_unavailable",
            lambda: self.service.test_connectivity(
                explicit_confirmation=True,
                source="two",
                probe=lambda _key: {"raw_response": 1},
            ),
        )
        self.assert_error(
            "service_unavailable",
            lambda: self.service.test_connectivity(
                explicit_confirmation=True,
                source="three",
                probe=lambda _key: {"requests": True},
            ),
        )

    def test_probe_exception_has_no_secret_cause_context_traceback_or_log(self) -> None:
        self.service.save(FAKE_KEY_ONE)
        records: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            def fail(key: str):
                raise RuntimeError("provider echoed " + key)

            try:
                self.service.test_connectivity(
                    explicit_confirmation=True, source="198.51.100.7", probe=fail
                )
                self.fail("probe failure must be rejected")
            except SecretStoreError as exc:
                self.assertEqual("service_unavailable", exc.code)
                self.assertIsNone(exc.__cause__)
                self.assertIsNone(exc.__context__)
                rendered = "".join(traceback.format_exception(exc))
                self.assertNotIn(FAKE_KEY_ONE, rendered)
                self.assertNotIn(FAKE_KEY_ONE, repr(exc.__cause__))
                self.assertNotIn(FAKE_KEY_ONE, repr(exc.__context__))
        finally:
            root.removeHandler(handler)
        self.assertNotIn(FAKE_KEY_ONE, "\n".join(r.getMessage() for r in records))

    def _application(self):
        auth_store = SqliteAuthStore(self.auth_path)
        auth = AuthService(auth_store, audit_key=b"A" * 32)
        grant = auth.create_bootstrap_grant()
        credentials = auth.bootstrap_admin(
            grant.token,
            "admin",
            "correct horse battery staple",
            request_id="req-bootstrap",
        )
        probe_calls: list[str] = []

        def probe(key: str):
            probe_calls.append(key)
            return {"requests": 1}

        app = SecretApplication(auth, self.service, connectivity_probe=probe)
        return app, credentials, probe_calls

    @staticmethod
    def _key_body(request_id: str, api_key: str = FAKE_KEY_ONE) -> bytes:
        return json.dumps(
            {
                "api_version": "3.0",
                "contract_version": "1.0.0",
                "request_id": request_id,
                "provider": PROVIDER,
                "model": MODEL,
                "api_key": api_key,
            }
        ).encode()

    def test_api_requires_admin_and_csrf_and_never_echoes_key(self) -> None:
        app, credentials, calls = self._application()
        denied = app.handle(
            "POST",
            "/api/v3/admin/secrets/bailian",
            request_id="req-denied",
            body=self._key_body("req-denied"),
        )
        self.assertEqual(401, denied.status)
        no_csrf = app.handle(
            "POST",
            "/api/v3/admin/secrets/bailian",
            request_id="req-csrf",
            body=self._key_body("req-csrf"),
            cookies={"ict_admin_session": credentials.token},
        )
        self.assertEqual(403, no_csrf.status)
        saved = app.handle(
            "POST",
            "/api/v3/admin/secrets/bailian",
            request_id="req-save",
            body=self._key_body("req-save"),
            cookies={"ict_admin_session": credentials.token},
            headers={"X-CSRF-Token": credentials.csrf_token},
        )
        self.assertEqual(200, saved.status)
        self.assertNotIn(FAKE_KEY_ONE.encode(), saved.body)
        self.assertEqual([], calls)

    def test_api_rejects_model_provider_request_id_and_unknown_fields(self) -> None:
        app, credentials, _ = self._application()
        base = json.loads(self._key_body("req-fixed"))
        cases = (
            {**base, "model": "another-model"},
            {**base, "provider": "another-provider"},
            {**base, "request_id": "req-other"},
            {**base, "base_url": "https://example.invalid"},
        )
        for index, payload in enumerate(cases):
            response = app.handle(
                "POST",
                "/api/v3/admin/secrets/bailian",
                request_id="req-fixed",
                body=json.dumps(payload).encode(),
                cookies={"ict_admin_session": credentials.token},
                headers={"X-CSRF-Token": credentials.csrf_token},
            )
            with self.subTest(index=index):
                self.assertEqual(400, response.status)
                self.assertEqual("invalid_request", json.loads(response.body)["error"]["code"])
        self.assertFalse(self.store.status().configured)

    def test_api_explicit_connectivity_delete_and_stable_storage_error(self) -> None:
        app, credentials, calls = self._application()
        auth = {"ict_admin_session": credentials.token}
        csrf = {"X-CSRF-Token": credentials.csrf_token}
        app.handle(
            "POST",
            "/api/v3/admin/secrets/bailian",
            request_id="req-save",
            body=self._key_body("req-save"),
            cookies=auth,
            headers=csrf,
        )
        test_response = app.handle(
            "POST",
            "/api/v3/admin/secrets/bailian/connectivity-test",
            request_id="req-test",
            body=json.dumps(
                {
                    "api_version": "3.0",
                    "contract_version": "1.0.0",
                    "request_id": "req-test",
                    "explicit_confirmation": True,
                }
            ).encode(),
            cookies=auth,
            headers=csrf,
        )
        self.assertEqual(200, test_response.status)
        self.assertEqual([FAKE_KEY_ONE], calls)
        deleted = app.handle(
            "DELETE",
            "/api/v3/admin/secrets/bailian",
            request_id="req-delete",
            cookies=auth,
            headers=csrf,
        )
        self.assertEqual(200, deleted.status)
        self.assertEqual("deleted", json.loads(deleted.body)["status"])
        Path(self.secret_path).write_bytes(b"not-a-sqlite-database")
        failed = app.handle(
            "GET",
            "/api/v3/admin/secrets/bailian",
            request_id="req-storage",
            cookies=auth,
        )
        self.assertEqual(503, failed.status)
        self.assertEqual("service_unavailable", json.loads(failed.body)["error"]["code"])
        self.assertNotIn("sqlite", failed.body.decode().casefold())

    def test_public_unconfigured_error_is_in_frozen_endpoint_catalog(self) -> None:
        app, credentials, calls = self._application()
        response = app.handle(
            "POST", "/api/v3/admin/secrets/bailian/connectivity-test",
            request_id="req-absent",
            body=json.dumps({
                "api_version": "3.0", "contract_version": "1.0.0",
                "request_id": "req-absent", "explicit_confirmation": True,
            }).encode(),
            cookies={"ict_admin_session": credentials.token},
            headers={"X-CSRF-Token": credentials.csrf_token},
        )
        contract = json.loads(
            Path("configs/contracts/d5/product-layer.v1.json").read_text("utf-8")
        )
        code = json.loads(response.body)["error"]["code"]
        endpoint = next(
            item for item in contract["endpoints"]
            if item["id"] == "admin_test_key_connectivity"
        )
        self.assertEqual(503, response.status)
        self.assertEqual("service_unavailable", code)
        self.assertIn(code, {item["code"] for item in contract["error_catalog"]})
        self.assertIn(code, endpoint["errors"])
        self.assertEqual([], calls)
        events = app.auth_service.store.audit_events()
        self.assertEqual("connectivity_tested", events[-1]["event_type"])
        self.assertEqual("failed", events[-1]["outcome"])

    def test_secret_and_connectivity_audit_is_content_safe(self) -> None:
        app, credentials, calls = self._application()
        auth = {"ict_admin_session": credentials.token}
        csrf = {"X-CSRF-Token": credentials.csrf_token}
        base = "/api/v3/admin/secrets/bailian"
        self.assertEqual(200, app.handle(
            "POST", base, request_id="req-save",
            body=self._key_body("req-save"), cookies=auth, headers=csrf,
        ).status)
        self.assertEqual(200, app.handle(
            "PUT", base, request_id="req-replace",
            body=self._key_body("req-replace", FAKE_KEY_TWO),
            cookies=auth, headers=csrf,
        ).status)
        connectivity = lambda request_id: app.handle(
            "POST", base + "/connectivity-test", request_id=request_id,
            body=json.dumps({
                "api_version": "3.0", "contract_version": "1.0.0",
                "request_id": request_id, "explicit_confirmation": True,
            }).encode(), cookies=auth, headers=csrf, source="198.51.100.3",
        )
        for index in range(3):
            self.assertEqual(200, connectivity(f"req-test-{index}").status)
        self.assertEqual(429, connectivity("req-limit").status)
        self.assertEqual(200, app.handle(
            "DELETE", base, request_id="req-delete", cookies=auth, headers=csrf,
        ).status)
        events = app.auth_service.store.audit_events()
        kinds = [event["event_type"] for event in events]
        self.assertEqual(3, kinds.count("secret_changed"))
        self.assertEqual(3, kinds.count("connectivity_tested"))
        self.assertEqual(1, kinds.count("security_limit_rejected"))
        self.assertEqual("rate_limited", events[-2]["error_code"])
        self.assertEqual(3, len(calls))
        serialized = json.dumps(events)
        for forbidden in (FAKE_KEY_ONE, FAKE_KEY_TWO, credentials.token, credentials.csrf_token):
            self.assertNotIn(forbidden, serialized)
        for event in events[1:]:
            self.assertEqual("admin", event["actor_role"])
            self.assertRegex(event["request_id"], r"^req_[0-9a-f]{64}$")

    def test_audit_write_failure_rolls_back_secret_mutations(self) -> None:
        app, credentials, _ = self._application()
        auth = {"ict_admin_session": credentials.token}
        csrf = {"X-CSRF-Token": credentials.csrf_token}
        base = "/api/v3/admin/secrets/bailian"

        def request(method: str, request_id: str, key: str | None = None):
            return app.handle(
                method, base, request_id=request_id,
                body=self._key_body(request_id, key) if key else b"{}",
                cookies=auth, headers=csrf,
            )

        with patch.object(SecretAudit, "append", side_effect=sqlite3.OperationalError("audit offline")):
            self.assertEqual(503, request("POST", "req-save-fail", FAKE_KEY_ONE).status)
        self.assertFalse(self.store.status().configured)
        self.assertEqual(200, request("POST", "req-save", FAKE_KEY_ONE).status)
        original = self.store.status()
        with patch.object(SecretAudit, "append", side_effect=sqlite3.OperationalError("audit offline")):
            self.assertEqual(503, request("PUT", "req-replace-fail", FAKE_KEY_TWO).status)
            self.assertEqual(503, request("DELETE", "req-delete-fail").status)
        self.assertEqual(original, self.store.status())
        self.assertEqual(FAKE_KEY_ONE, self.store.get_for_provider_call())
        events = app.auth_service.store.audit_events()
        self.assertEqual(1, sum(e["event_type"] == "secret_changed" for e in events))

    def test_cross_database_audit_refuses_nonatomic_wal_mode(self) -> None:
        app, credentials, _ = self._application()
        with closing(sqlite3.connect(self.auth_path)) as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        response = app.handle(
            "POST", "/api/v3/admin/secrets/bailian", request_id="req-wal",
            body=self._key_body("req-wal"),
            cookies={"ict_admin_session": credentials.token},
            headers={"X-CSRF-Token": credentials.csrf_token},
        )
        self.assertEqual(503, response.status)
        self.assertFalse(self.store.status().configured)
        self.assertEqual(0, sum(
            e["event_type"] == "secret_changed"
            for e in app.auth_service.store.audit_events()
        ))

    def test_limit_audit_failure_rolls_back_limit_and_allows_safe_retry(self) -> None:
        app, credentials, calls = self._application()
        auth = {"ict_admin_session": credentials.token}
        csrf = {"X-CSRF-Token": credentials.csrf_token}
        base = "/api/v3/admin/secrets/bailian"
        self.assertEqual(200, app.handle(
            "POST", base, request_id="req-save", body=self._key_body("req-save"),
            cookies=auth, headers=csrf,
        ).status)

        def attempt(request_id: str):
            return app.handle(
                "POST", base + "/connectivity-test", request_id=request_id,
                body=json.dumps({
                    "api_version": "3.0", "contract_version": "1.0.0",
                    "request_id": request_id, "explicit_confirmation": True,
                }).encode(), cookies=auth, headers=csrf, source="198.51.100.9",
            )

        for index in range(3):
            self.assertEqual(200, attempt(f"req-{index}").status)
        with patch.object(SecretAudit, "append", side_effect=sqlite3.OperationalError("audit offline")):
            self.assertEqual(503, attempt("req-limit-fail").status)
        self.assertEqual(429, attempt("req-limit-retry").status)
        self.assertEqual(3, len(calls))
        events = app.auth_service.store.audit_events()
        self.assertEqual(1, sum(
            event["event_type"] == "security_limit_rejected" for event in events
        ))

    def test_connectivity_failure_is_audited_without_provider_exception(self) -> None:
        app, credentials, _ = self._application()
        app.connectivity_probe = lambda key: (_ for _ in ()).throw(
            RuntimeError("provider echoed " + key)
        )
        auth = {"ict_admin_session": credentials.token}
        csrf = {"X-CSRF-Token": credentials.csrf_token}
        self.assertEqual(200, app.handle(
            "POST", "/api/v3/admin/secrets/bailian", request_id="req-save",
            body=self._key_body("req-save"), cookies=auth, headers=csrf,
        ).status)
        response = app.handle(
            "POST", "/api/v3/admin/secrets/bailian/connectivity-test",
            request_id="req-failed-probe",
            body=json.dumps({
                "api_version": "3.0", "contract_version": "1.0.0",
                "request_id": "req-failed-probe", "explicit_confirmation": True,
            }).encode(), cookies=auth, headers=csrf,
        )
        self.assertEqual(503, response.status)
        self.assertEqual("service_unavailable", json.loads(response.body)["error"]["code"])
        self.assertNotIn(FAKE_KEY_ONE.encode(), response.body)
        events = app.auth_service.store.audit_events()
        self.assertEqual("connectivity_tested", events[-1]["event_type"])
        self.assertEqual("failed", events[-1]["outcome"])
        self.assertNotIn(FAKE_KEY_ONE, json.dumps(events))


if __name__ == "__main__":
    unittest.main()
