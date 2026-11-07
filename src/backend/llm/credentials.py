"""Explicit, non-scanning credential loading for local development."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping, Protocol


class CredentialError(RuntimeError):
    """A credential-loading failure that never embeds credential content."""


@dataclass(frozen=True)
class Credential:
    value: str = field(repr=False)
    source_name: str

    def __repr__(self) -> str:
        return f"Credential(value='[REDACTED]', source_name={self.source_name!r})"


class CredentialProvider(Protocol):
    def load(self) -> Credential:
        """Load one credential without logging it."""


def _validate_key(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise CredentialError("Credential must be nonempty, bounded, header-safe text")
    return value


def _load_csv_key(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            table = list(csv.reader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CredentialError("Credential CSV could not be read") from exc
    if not table:
        raise CredentialError("Credential CSV has no rows")

    headers = table[0]
    rows = table[1:]
    for column_index, field_name in enumerate(headers):
        normalized = re.sub(
            r"[^a-z0-9\u4e00-\u9fff]", "", field_name.strip().lower()
        )
        if "apikey" in normalized or "密钥" in normalized or normalized == "key":
            for row in rows:
                if column_index < len(row) and row[column_index].strip():
                    return _validate_key(row[column_index])

    for row in table:
        for value in row:
            candidate = value.strip().lstrip("\ufeff")
            if re.fullmatch(r"sk-[A-Za-z0-9._-]{16,}", candidate):
                return _validate_key(candidate)
    raise CredentialError("Credential CSV does not contain an approved key field")


class ExternalFileCredentialProvider:
    """Load from exactly two approved paths; never search other files."""

    def __init__(self, preferred_path: str | Path, fallback_path: str | Path) -> None:
        self.preferred_path = Path(preferred_path)
        self.fallback_path = Path(fallback_path)

    @classmethod
    def from_config(
        cls, credential_config: Mapping[str, Any]
    ) -> "ExternalFileCredentialProvider":
        if credential_config.get("source") != "external_file":
            raise CredentialError("Only external_file credentials are allowed")
        paths = [credential_config.get(name) for name in ("preferred_path", "fallback_path")]
        if any(not isinstance(path, (str, Path)) or not str(path).strip() for path in paths):
            raise CredentialError("Configure both approved credential paths explicitly")
        return cls(
            preferred_path=paths[0],
            fallback_path=paths[1],
        )

    def load(self) -> Credential:
        if self.preferred_path.is_file():
            try:
                key = _validate_key(
                    self.preferred_path.read_text(encoding="utf-8-sig")
                )
                return Credential(key, self.preferred_path.name)
            except (OSError, UnicodeError, CredentialError):
                pass
        if self.fallback_path.is_file():
            key = _load_csv_key(self.fallback_path)
            return Credential(key, self.fallback_path.name)
        raise CredentialError("No valid credential exists at the approved paths")


__all__ = [
    "Credential",
    "CredentialError",
    "CredentialProvider",
    "ExternalFileCredentialProvider",
]
