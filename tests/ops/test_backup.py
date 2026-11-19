import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import zstandard

from tests.imported_jobs import ingest, make_store, record

NOW = datetime(2026, 10, 20, 3, 30, tzinfo=timezone.utc)


def test_backup_is_consistent_and_verifiable(tmp_path):
    from auto_app.ops.backup import backup, verify

    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer")])
    writer = sqlite3.connect(tmp_path / "data/auto_app.db")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE jobs SET notified_at=NULL")
    try:
        saved = backup(tmp_path / "data/auto_app.db", tmp_path / "backups", now=NOW)
    finally:
        writer.rollback()
        writer.close()
        store.engine.dispose()
    assert saved["quick_check"] == "ok"
    assert os.stat(saved["path"]).st_mode & 0o777 == 0o600
    assert verify(saved["path"])["jobs"] == 1
    manifest = tmp_path / "backups/manifest.jsonl"
    assert json.loads(manifest.read_text())["sha256"] == saved["sha256"]
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "backups").stat().st_mode & 0o777 == 0o700


def test_new_archive_detects_payload_corruption_without_a_manifest(tmp_path):
    from auto_app.ops.backup import backup, verify

    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer")])
    # Incompressible payload survives as a raw block; changing a value leaves
    # SQLite structurally valid, so only archive integrity can catch this.
    payload = random.Random(42).randbytes(256000)
    with sqlite3.connect(tmp_path / "data/auto_app.db") as conn:
        conn.execute("UPDATE posting_raw SET page_text=?", (zstandard.ZstdCompressor().compress(payload),))
    saved = backup(tmp_path / "data/auto_app.db", tmp_path / "backups", now=NOW)
    store.engine.dispose()
    path = Path(saved["path"])
    encoded = bytearray(path.read_bytes())
    offset = encoded.find(payload[100000:100040])
    assert offset >= 0
    encoded[offset + 10] ^= 1
    path.write_bytes(encoded)
    (path.parent / "manifest.jsonl").unlink()
    with pytest.raises(zstandard.ZstdError, match="checksum"):
        verify(path)


@pytest.mark.parametrize("copied", [False, True])
def test_verify_rejects_a_changed_valid_database_using_manifest(tmp_path, copied):
    from auto_app.ops.backup import backup, verify

    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer")])
    saved = backup(tmp_path / "data/auto_app.db", tmp_path / "backups", now=NOW)
    store.engine.dispose()
    path = Path(saved["path"])
    if copied:
        shutil.copytree(path.parent, tmp_path / "off-host")
        path = tmp_path / "off-host" / path.name
    with sqlite3.connect(tmp_path / "data/auto_app.db") as conn:
        conn.execute("UPDATE jobs SET notified_at='2026-10-20T01:00:00Z'")
    path.write_bytes(zstandard.ZstdCompressor(write_checksum=True).compress(
        (tmp_path / "data/auto_app.db").read_bytes()))
    with pytest.raises(ValueError, match="manifest.*SHA-256"):
        verify(path)


@pytest.mark.parametrize("damage", ["json", "missing_hash", "invalid_hash", "size", "conflict", "duplicate_hash"])
def test_verify_rejects_malformed_or_conflicting_manifest_metadata(tmp_path, damage):
    from auto_app.ops.backup import backup, verify

    store = make_store(tmp_path)
    saved = backup(tmp_path / "data/auto_app.db", tmp_path / "backups", now=NOW)
    store.engine.dispose()
    manifest = tmp_path / "backups/manifest.jsonl"
    altered = saved.copy()
    if damage == "json":
        content = '{"path":'
    elif damage == "duplicate_hash":
        content = '{"sha256":"' + "0" * 64 + '",' + json.dumps(saved)[1:] + "\n"
    else:
        if damage == "missing_hash":
            del altered["sha256"]
        elif damage == "invalid_hash":
            altered["sha256"] = "not a sha256"
        elif damage == "size":
            altered["bytes"] += 1
        else:
            altered["sha256"] = "0" * 64
        content = (json.dumps(saved) + "\n" if damage == "conflict" else "") + json.dumps(altered) + "\n"
    manifest.write_text(content)
    with pytest.raises(ValueError, match="manifest"):
        verify(saved["path"])


def test_verify_accepts_copied_archives_and_legacy_archives_without_manifest(tmp_path):
    from auto_app.ops.backup import backup, verify

    store = make_store(tmp_path)
    saved = backup(tmp_path / "data/auto_app.db", tmp_path / "backups", now=NOW)
    store.engine.dispose()
    shutil.copytree(tmp_path / "backups", tmp_path / "off-host")
    assert verify(tmp_path / "off-host" / Path(saved["path"]).name)["ok"] is True
    legacy = tmp_path / "legacy.db.zst"
    legacy.write_bytes(zstandard.ZstdCompressor().compress((tmp_path / "data/auto_app.db").read_bytes()))
    assert verify(legacy)["ok"] is True


@pytest.mark.parametrize("damage", ["truncated_checksum", "trailing_data"])
def test_verify_requires_a_complete_archive_frame_without_manifest(tmp_path, damage):
    from auto_app.ops.backup import backup, verify

    store = make_store(tmp_path)
    saved = backup(tmp_path / "data/auto_app.db", tmp_path / "backups", now=NOW)
    store.engine.dispose()
    path = Path(saved["path"])
    encoded = path.read_bytes()
    path.write_bytes(encoded[:-1] if damage == "truncated_checksum" else encoded + b"\x00")
    (path.parent / "manifest.jsonl").unlink()
    with pytest.raises((ValueError, zstandard.ZstdError)):
        verify(path)


def test_same_second_backup_never_overwrites(tmp_path):
    from auto_app.ops.backup import backup

    store = make_store(tmp_path)
    first = backup(tmp_path / "data/auto_app.db", tmp_path / "backups", now=NOW)
    with pytest.raises(FileExistsError):
        backup(tmp_path / "data/auto_app.db", tmp_path / "backups", now=NOW)
    assert (tmp_path / "backups/manifest.jsonl").read_text().count(first["sha256"]) == 1
    store.engine.dispose()


def test_failed_backup_publishes_no_output(tmp_path):
    from auto_app.ops.backup import backup

    db = tmp_path / "broken.db"
    db.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        backup(db, tmp_path / "backups", now=NOW)
    assert list((tmp_path / "backups").iterdir()) == []


def test_verify_rejects_incompatible_database(tmp_path):
    from auto_app.ops.backup import verify
    from auto_app.query.service import QueryError

    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE nonsense (x)")
    path = tmp_path / "copy.zst"
    path.write_bytes(zstandard.ZstdCompressor().compress(db.read_bytes()))
    with pytest.raises(QueryError, match="Incompatible database schema"):
        verify(path)


def test_verify_runs_integrity_check(tmp_path, monkeypatch):
    from auto_app.ops import backup as module

    store = make_store(tmp_path)
    saved = module.backup(tmp_path / "data/auto_app.db", tmp_path / "backups", now=NOW)
    store.engine.dispose()
    called = []
    check = module._check_integrity
    monkeypatch.setattr(module, "_check_integrity", lambda conn: (called.append(True), check(conn))[1])
    assert module.verify(saved["path"])["ok"] is True
    assert called == [True]


def test_prune_keeps_the_newest_daily_and_weekly_copies(tmp_path):
    from auto_app.ops.backup import prune

    names = []
    for days in range(120):
        for hour in (3, 15):
            stamp = (NOW - timedelta(days=days)).replace(hour=hour)
            name = f"auto_app-{stamp:%Y%m%dT%H%M%SZ}.db.zst"
            (tmp_path / name).write_bytes(b"x")
            names.append(name)
    removed = prune(tmp_path, keep_daily=14, keep_weekly=8, now=NOW)
    kept = sorted(p.name for p in tmp_path.glob("auto_app-*.db.zst"))
    assert f"auto_app-{NOW.replace(hour=15):%Y%m%dT%H%M%SZ}.db.zst" in kept
    assert len([k for k in kept if k >= f"auto_app-{NOW - timedelta(days=13):%Y%m%d}"]) == 14
    assert 14 < len(kept) <= 14 + 8
    assert len(removed) == len(names) - len(kept)


def test_prune_ignores_unrecognized_files_directories_and_symlinks(tmp_path):
    from auto_app.ops.backup import prune

    latest = tmp_path / f"auto_app-{NOW:%Y%m%dT%H%M%SZ}.db.zst"
    latest.write_bytes(b"x")
    unknown = tmp_path / "auto_app-not-a-date.db.zst"
    unknown.write_bytes(b"x")
    directory = tmp_path / "auto_app-20200101T000000Z.db.zst"
    directory.mkdir()
    link = tmp_path / "auto_app-20200102T000000Z.db.zst"
    link.symlink_to(unknown)
    assert prune(tmp_path, keep_daily=0, keep_weekly=0, now=NOW) == []
    assert all(p.exists() for p in (latest, unknown, directory, link))


@pytest.mark.parametrize("daily,weekly", [(-1, 1), (1, -1)])
def test_prune_rejects_negative_retention(tmp_path, daily, weekly):
    from auto_app.ops.backup import prune

    with pytest.raises(ValueError):
        prune(tmp_path, keep_daily=daily, keep_weekly=weekly)


def test_preparation_backup_and_retention_are_independent(tmp_path):
    from auto_app.ops.backup import backup, prune, verify
    from auto_app.prepare.store import RunStore

    ledger = RunStore(tmp_path / "preparation")
    store = make_store(tmp_path)
    dest = tmp_path / "backups"
    main = backup(tmp_path / "data/auto_app.db", dest, now=NOW)
    prep = backup(ledger.path, dest, now=NOW)
    assert "preparation-" in prep["path"]
    assert verify(prep["path"])["preparation_runs"] == 0
    assert prune(dest, keep_daily=0, keep_weekly=0, now=NOW) == []
    assert all(os.path.exists(entry["path"]) for entry in (main, prep))
    store.engine.dispose()


def test_backup_cli_includes_optional_preparation_ledger(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from auto_app.ops.cli import app
    from auto_app.prepare.store import RunStore

    store = make_store(tmp_path)
    RunStore(tmp_path / "preparation")
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["backup"])
    assert result.exit_code == 0, result.output
    saved = json.loads(result.output)
    assert saved["verify"]["jobs"] == 0
    assert saved["preparation"]["verify"]["preparation_runs"] == 0
    store.engine.dispose()


def test_manifest_concurrent_writers_preserve_every_entry(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import fcntl
    import threading
    from auto_app.ops.backup import _manifest

    barrier = threading.Barrier(3)
    flock = fcntl.flock

    def synchronized_lock(fd, operation):
        if operation == fcntl.LOCK_EX:
            barrier.wait(timeout=5)
        flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", synchronized_lock)
    with ThreadPoolExecutor(max_workers=3) as workers:
        futures = [workers.submit(_manifest, tmp_path, {"id": n}) for n in range(3)]
        for future in futures:
            future.result(timeout=10)
    assert sorted(json.loads(line)["id"] for line in (tmp_path / "manifest.jsonl").read_text().splitlines()) == [0, 1, 2]


def test_prune_refuses_a_symlinked_directory(tmp_path):
    from auto_app.ops.backup import prune

    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "backups"
    link.symlink_to(actual)
    with pytest.raises(ValueError, match="symlink"):
        prune(link)


def make_kit(root):
    kit = root / "apply-kit"
    (kit / "jobs" / "42").mkdir(parents=True)
    (kit / "jobs" / "42" / "resume.pdf").write_bytes(b"%PDF-1.7 fictional\n")
    (kit / "answers.md").write_text("# cheat sheet (fictional)\n")
    (kit / "jobs" / "index.json").write_text('{"jobs": []}')
    (kit / "outside-link").symlink_to(root)  # never followed or archived
    return kit


def test_apply_kit_archive_is_verifiable_and_pruned_on_its_own(tmp_path):
    from auto_app.ops.backup import backup_tree, prune, verify_tree

    kit, dest = make_kit(tmp_path), tmp_path / "backups"
    entry = backup_tree(kit, dest, now=NOW)
    assert Path(entry["path"]).name == f"apply_kit-{NOW:%Y%m%dT%H%M%SZ}.tar.zst"
    assert oct(Path(entry["path"]).stat().st_mode & 0o777) == "0o600"
    assert verify_tree(entry["path"]) == {"ok": True, "files": 3, "bytes": entry["content_bytes"]}
    later = backup_tree(kit, dest, now=NOW + timedelta(days=1))
    assert prune(dest, keep_daily=1, keep_weekly=0, now=NOW + timedelta(days=1)) == [Path(entry["path"]).name]
    assert os.path.exists(later["path"])
    damaged = Path(later["path"])
    data = bytearray(damaged.read_bytes())
    data[len(data) // 2] ^= 0xFF
    damaged.write_bytes(bytes(data))
    with pytest.raises(ValueError):
        verify_tree(damaged)


def test_backup_cli_includes_the_apply_kit_next_to_the_runtime(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from auto_app.ops.cli import app

    runtime = tmp_path / "runtime"
    store = make_store(runtime)
    make_kit(tmp_path)
    monkeypatch.setenv("AUTO_APP_HOME", str(runtime))
    result = CliRunner().invoke(app, ["backup"])
    assert result.exit_code == 0, result.output
    saved = json.loads(result.output)
    assert saved["apply_kit"]["verify"]["files"] == 3
    assert CliRunner().invoke(app, ["verify", saved["apply_kit"]["path"]]).exit_code == 0
    store.engine.dispose()
