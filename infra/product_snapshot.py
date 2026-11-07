"""Stopped-instance backup/restore. Never includes the external master key."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


DB_NAMES = ("metadata.sqlite3", "secrets.sqlite3")


def _outside_checkout(path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    path = path.expanduser().resolve()
    if path == root or root in path.parents:
        raise ValueError("invalid_directory")
    return path


def _check_db(path: Path) -> None:
    if not path.is_file():
        raise ValueError("storage_missing")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("storage_corrupt")
    finally:
        connection.close()


def _check_documents(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        raise ValueError("storage_missing")
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValueError("snapshot_invalid")


def _file_hashes(root: Path) -> dict[str, str]:
    paths = [root / name for name in DB_NAMES]
    paths.extend(path for path in (root / "documents").rglob("*") if path.is_file())
    hashes: dict[str, str] = {}
    for path in paths:
        if path.is_symlink():
            raise ValueError("snapshot_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[path.relative_to(root).as_posix()] = digest.hexdigest()
    return dict(sorted(hashes.items()))


def backup(instance: str | Path, target: str | Path) -> None:
    """Copy only the two SQLite databases and document tree into a new target."""
    source, target = _outside_checkout(Path(instance)), _outside_checkout(Path(target))
    if target.exists() or target == source or source in target.parents or target in source.parents:
        raise ValueError("invalid_directory")
    for name in DB_NAMES:
        _check_db(source / name)
    _check_documents(source / "documents")
    target.mkdir(parents=True)
    try:
        for name in DB_NAMES:
            shutil.copy2(source / name, target / name)
            _check_db(target / name)
        shutil.copytree(source / "documents", target / "documents", symlinks=False)
        _check_documents(target / "documents")
        (target / "snapshot.json").write_text(
            json.dumps({"format": 2, "included": [*DB_NAMES, "documents"],
                        "master_key_included": False, "files_sha256": _file_hashes(target)},
                       sort_keys=True), encoding="utf-8"
        )
    except BaseException:
        # Preserve partial target for diagnosis; never touch the source.
        raise


def restore(snapshot: str | Path, target: str | Path) -> None:
    """Restore into a new empty location; never overwrite a live instance."""
    source, target = _outside_checkout(Path(snapshot)), _outside_checkout(Path(target))
    if target.exists() or target == source or source in target.parents or target in source.parents:
        raise ValueError("invalid_directory")
    try:
        manifest = json.loads((source / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("snapshot_invalid") from exc
    if (not isinstance(manifest, dict) or set(manifest) !=
            {"format", "included", "master_key_included", "files_sha256"} or
            manifest["format"] != 2 or manifest["included"] != [*DB_NAMES, "documents"] or
            manifest["master_key_included"] is not False or not isinstance(manifest["files_sha256"], dict)):
        raise ValueError("snapshot_invalid")
    for name in DB_NAMES:
        _check_db(source / name)
    _check_documents(source / "documents")
    if _file_hashes(source) != manifest["files_sha256"]:
        raise ValueError("snapshot_invalid")
    target.mkdir(parents=True)
    for name in DB_NAMES:
        shutil.copy2(source / name, target / name)
    shutil.copytree(source / "documents", target / "documents", symlinks=False)
    if _file_hashes(target) != manifest["files_sha256"]:
        raise ValueError("snapshot_invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()
    try:
        (backup if args.command == "backup" else restore)(args.source, args.target)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("操作失败：" + (str(exc) if isinstance(exc, ValueError) else "service_unavailable"))
        return 1
    print("操作完成。主密钥不在快照中，恢复后必须从原外部密钥源提供。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
