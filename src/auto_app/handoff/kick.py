"""The kick file: touched when the package runner has new work, so systemd starts it at once.

deploy/jd-apply-queue.path watches $AUTO_APP_HOME/data/apply-queue.kick and starts jd-apply-queue.service, which
makes and publishes packages (it never submits). The hourly timer stays as the fallback, so a kick that cannot be
written only delays the work; it never fails the request that caused it.
"""

import os
from pathlib import Path

KICK_FILE = "apply-queue.kick"


def kick(home: Path) -> None:
    """Update the kick file's time (creating it), ignoring any error."""
    path = Path(home) / "data" / KICK_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b"")  # a write closes as IN_CLOSE_WRITE, which PathChanged= watches for
        finally:
            os.close(fd)
        os.utime(path)
    except OSError:
        pass
