"""The package that went out with an application, kept apart from the live one.

apply-kit/jobs/<job>/ is the live package: jd-resume-tailor's `publish --replace` overwrites it. When a job is
submitted (an agent's `submitted` outcome, or the owner's own "applied"), its files are copied once to
apply-kit/submitted/<job>/ with frozen.json (each file's sha256 and size, the time, and whether it was copied later
by the backfill). The first copy is kept; nothing here ever raises, so a copy that cannot be made never stops a real
submission from being recorded.
"""

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SUBMITTED_DIR = "submitted"
FROZEN_META = "frozen.json"
MAX_FILE_BYTES = 10 * 1024 * 1024


def _files():
    from auto_app.handoff.service import KIT_FILES  # the package's file names, one list for every reader

    return KIT_FILES


def frozen_at(kit_dir: Path, job_id: int) -> str | None:
    """When the job's package was frozen, or None."""
    try:
        meta = json.loads((Path(kit_dir) / SUBMITTED_DIR / str(job_id) / FROZEN_META).read_text())
        return meta.get("frozen_at") if isinstance(meta, dict) else None
    except (OSError, ValueError):
        return None


def freeze_package(kit_dir: Path, job_id: int, now: datetime | None = None, *, backfilled: bool = False
                   ) -> Path | None:
    """Copy apply-kit/jobs/<job>/'s package files to apply-kit/submitted/<job>/ once; the copy's folder, or None."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    kit_dir = Path(kit_dir)
    source = kit_dir / "jobs" / str(job_id)
    root = kit_dir / SUBMITTED_DIR
    target = root / str(job_id)
    try:
        if os.path.lexists(target):
            return target if target.is_dir() and not target.is_symlink() else None
        if source.is_symlink() or not source.is_dir() or kit_dir.is_symlink():
            return None
        names = [n for n in _files() if (source / n).is_file() and not (source / n).is_symlink()
                 and (source / n).stat().st_size <= MAX_FILE_BYTES]
        if not names:
            return None
        if root.is_symlink():
            return None
        root.mkdir(mode=0o755, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".freeze-{job_id}-", dir=root))
        try:
            files = {}
            for name in names:
                data = (source / name).read_bytes()
                (stage / name).write_bytes(data)
                files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            meta = {"job_id": job_id, "frozen_at": now.isoformat(), "backfilled": backfilled, "files": files}
            (stage / FROZEN_META).write_text(json.dumps(meta, indent=2) + "\n")
            stage.chmod(0o755)
            os.rename(stage, target)  # fails if another copy got there first; that one is kept
        except OSError:
            shutil.rmtree(stage, ignore_errors=True)
            return target if target.is_dir() else None
        return target
    except OSError:
        return None
