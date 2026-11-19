"""SQLite online backups and an archive of the apply-kit folder, as private atomic files with per-kind retention."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tarfile
import tempfile

import zstandard

NAME = re.compile(r"(auto_app|preparation)-(\d{8}T\d{6}Z)\.db\.zst|(apply_kit)-(\d{8}T\d{6}Z)\.tar\.zst")
PREPARATION_COLUMNS = set("run_id idempotency_key status job_id title company apply_url candidate created_at updated_at attempt result".split())


def _utc(now):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("backup timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _check_integrity(conn):
    results = conn.execute("PRAGMA quick_check").fetchall()
    if results != [("ok",)]:
        raise RuntimeError("backup failed its integrity check")
    return "ok"


def backup(db_path: Path, dest: Path, *, now: datetime | None = None) -> dict:
    """Publish one complete archive; same-second collisions fail without overwriting."""
    now, db_path, dest = _utc(now), Path(db_path), Path(dest)
    if dest.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest.chmod(0o700)
    prefix = "preparation" if db_path.name == "preparation.sqlite3" else "auto_app"
    target = dest / f"{prefix}-{now:%Y%m%dT%H%M%SZ}.db.zst"
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    with tempfile.TemporaryDirectory(dir=dest) as work:
        copy, compressed = Path(work) / "copy.db", Path(work) / "copy.zst"
        # Opening in read-only mode prevents accidental creation of a missing DB.
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(copy)) as destination:
                source.backup(destination)
                check = _check_integrity(destination)
        with copy.open("rb") as source, compressed.open("xb") as output:
            compressed.chmod(0o600)
            zstandard.ZstdCompressor(level=10, write_checksum=True).copy_stream(source, output)
            output.flush()
            os.fsync(output.fileno())
        digest = hashlib.sha256(compressed.read_bytes()).hexdigest()
        entry = {"path": str(target), "sha256": digest, "bytes": compressed.stat().st_size,
                 "created_at": now.isoformat(), "quick_check": check}
        # Atomic hard-link publication has no replacement race; incomplete files
        # never appear under a recognized archive name.
        os.link(compressed, target)
        try:
            _manifest(dest, entry)
        except BaseException:
            target.unlink()
            raise
    return entry


def _stream_end(decoder, source, output):
    """Decompress `source` into `output`, requiring one complete zstd frame and nothing after it."""
    while chunk := source.read(64 * 1024):
        output.write(decoder.decompress(chunk))
        if decoder.eof:
            if decoder.unused_data or source.read(1):
                raise ValueError("Backup archive contains trailing data")
            return
    raise ValueError("Backup archive is truncated")


def backup_tree(folder: Path, dest: Path, *, now: datetime | None = None) -> dict:
    """Archive a folder (the apply-kit: published packages, frozen submitted copies, the cheat sheet) as one
    tar.zst. Regular files and folders only: symlinks and anything else are left out, never followed."""
    now, folder, dest = _utc(now), Path(folder), Path(dest)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("the folder to back up must be a real directory")
    if dest.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest.chmod(0o700)
    target = dest / f"apply_kit-{now:%Y%m%dT%H%M%SZ}.tar.zst"
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    count = size = 0
    with tempfile.TemporaryDirectory(dir=dest) as work:
        tar_path, compressed = Path(work) / "copy.tar", Path(work) / "copy.zst"
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as archive:
            for root, dirs, files in os.walk(folder, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
                for name in sorted(files):
                    path = Path(root) / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    archive.add(path, arcname=str(path.relative_to(folder)), recursive=False)
                    count += 1
                    size += path.stat().st_size
        with tar_path.open("rb") as source, compressed.open("xb") as output:
            compressed.chmod(0o600)
            zstandard.ZstdCompressor(level=10, write_checksum=True).copy_stream(source, output)
            output.flush()
            os.fsync(output.fileno())
        digest = hashlib.sha256(compressed.read_bytes()).hexdigest()
        entry = {"path": str(target), "sha256": digest, "bytes": compressed.stat().st_size,
                 "created_at": now.isoformat(), "files": count, "content_bytes": size}
        os.link(compressed, target)
        try:
            _manifest(dest, entry)
        except BaseException:
            target.unlink()
            raise
    return entry


def verify_tree(path: Path) -> dict:
    """Check the manifest and the zstd frame, then read every member of a folder archive (nothing is extracted)."""
    path = Path(path)
    with tempfile.TemporaryDirectory() as work:
        tar_path = Path(work) / "copy.tar"
        with path.open("rb") as source, tar_path.open("xb") as output:
            _verify_manifest(path, source)
            _stream_end(zstandard.ZstdDecompressor().decompressobj(), source, output)
        count = size = 0
        try:
            with tarfile.open(tar_path, "r") as archive:
                for member in archive:
                    if member.isfile():
                        data = archive.extractfile(member)
                        while data.read(64 * 1024):
                            pass
                        count += 1
                        size += member.size
                    elif not member.isdir():
                        raise ValueError("Backup archive holds something other than files and folders")
        except tarfile.TarError as exc:
            raise ValueError("Backup archive is not a readable tar file") from exc
    return {"ok": True, "files": count, "bytes": size}


def _manifest(dest, entry):
    import fcntl

    manifest = dest / "manifest.jsonl"
    # Lock the stable directory inode: locking a manifest that gets replaced
    # would let waiting writers read an obsolete inode and lose entries.
    fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if manifest.exists() or manifest.is_symlink():
            source_fd = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(source_fd, encoding="utf-8") as source:
                original = source.read()
        else:
            original = ""
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=dest, delete=False) as output:
            scratch = Path(output.name)
            try:
                output.write(original)
                output.write(json.dumps(entry) + "\n")
                output.flush()
                os.fsync(output.fileno())
                os.replace(scratch, manifest)
                os.fsync(fd)
            finally:
                scratch.unlink(missing_ok=True)
    finally:
        os.close(fd)


def _verify_manifest(path, source):
    def unique_fields(pairs):
        entry = {}
        for key, value in pairs:
            if key in entry:
                raise ValueError("Duplicate backup manifest field")
            entry[key] = value
        return entry

    manifest = path.parent / "manifest.jsonl"
    if not manifest.exists() and not manifest.is_symlink():
        return  # Legacy archives and copies without a manifest remain readable.
    matches = []
    with manifest.open(encoding="utf-8") as entries:
        for number, line in enumerate(entries, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line, object_pairs_hook=unique_fields)
            except ValueError as exc:
                raise ValueError(f"Invalid backup manifest at line {number}") from exc
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
                raise ValueError(f"Invalid backup manifest path at line {number}")
            # Off-host copies retain the original absolute path in the manifest.
            # Archive names identify copies independently of their parent folder.
            if Path(entry["path"]).name != path.name:
                continue
            digest = entry.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                raise ValueError(f"Invalid backup manifest SHA-256 at line {number}")
            if "bytes" in entry and (type(entry["bytes"]) is not int or entry["bytes"] < 0):
                raise ValueError(f"Invalid backup manifest size at line {number}")
            matches.append(entry)
    if matches:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        size = os.fstat(source.fileno()).st_size
        source.seek(0)
        # Check every match: conflicting duplicate entries must not mask damage.
        for entry in matches:
            if entry["sha256"].lower() != digest:
                raise ValueError("Backup manifest SHA-256 does not match the archive")
            if "bytes" in entry and entry["bytes"] != size:
                raise ValueError("Backup manifest size does not match the archive")


def verify(path: Path) -> dict:
    """Check available archive metadata, then decompress and validate the database."""
    from auto_app.query.service import QueryService

    path = Path(path)
    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / "data/auto_app.db"
        copy.parent.mkdir(mode=0o700)
        with path.open("rb") as source, copy.open("xb") as output:
            copy.chmod(0o600)
            _verify_manifest(path, source)
            # copy_stream accepts a truncated frame even when its missing footer
            # contains the checksum. Require EOF explicitly before trusting it.
            decoder = zstandard.ZstdDecompressor().decompressobj()
            while chunk := source.read(64 * 1024):
                output.write(decoder.decompress(chunk))
                if decoder.eof:
                    if decoder.unused_data or source.read(1):
                        raise ValueError("Backup archive contains trailing data")
                    break
            if not decoder.eof:
                raise ValueError("Backup archive is truncated")
        with closing(sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)) as conn:
            _check_integrity(conn)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "preparation_runs" in tables and "jobs" not in tables:
                columns = {row[1] for row in conn.execute('PRAGMA table_info("preparation_runs")')}
                if not PREPARATION_COLUMNS <= columns:
                    raise ValueError("Incompatible preparation schema")
                count = conn.execute("SELECT COUNT(*) FROM preparation_runs").fetchone()[0]
                return {"ok": True, "preparation_runs": count, "schema_version": "preparation_runs-v1"}
        with QueryService(copy) as query:
            context = query.context()
    return {"ok": True, "jobs": context["counts"]["total"], "schema_version": context["schema_version"]}


def prune(dest: Path, *, keep_daily: int = 14, keep_weekly: int = 8, now: datetime | None = None) -> list[str]:
    """Keep latest per DB, recent calendar-day copies, then older ISO-week copies."""
    if type(keep_daily) is not int or type(keep_weekly) is not int or min(keep_daily, keep_weekly) < 0:
        raise ValueError("retention counts must be nonnegative integers")
    now, dest = _utc(now), Path(dest)
    if dest.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    groups = {}
    for path in dest.iterdir() if dest.exists() else ():
        match = NAME.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        kind, when = (match[1], match[2]) if match[1] else (match[3], match[4])
        try:
            stamp = datetime.strptime(when, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        groups.setdefault(kind, []).append((stamp, path))
    removed = []
    cutoff = now.date() - timedelta(days=max(0, keep_daily - 1))
    for files in groups.values():
        files.sort(reverse=True)
        keep, days, weeks = {files[0][1]}, set(), set()
        for stamp, path in files:
            day, week = stamp.date(), stamp.isocalendar()[:2]
            if keep_daily and day >= cutoff:
                if day not in days:
                    days.add(day)
                    keep.add(path)
            elif week not in weeks and len(weeks) < keep_weekly:
                weeks.add(week)
                keep.add(path)
        for _, path in files:
            if path not in keep:
                # Recheck type immediately before unlink: never follow links.
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                    removed.append(path.name)
    return removed
