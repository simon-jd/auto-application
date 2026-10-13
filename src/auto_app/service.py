"""Generate Linux service configuration without installing or starting it."""

from pathlib import Path


def _quote(value: str) -> str:
    """Quote one systemd word (Environment or ExecStart), not a shell word."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def build_systemd_unit(project: Path, home: Path, uv: str) -> str:
    """Build a system unit using an explicit uv executable and runtime home."""
    for value in (str(project), str(home), uv):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("service paths must not contain control characters")
    # A target Linux path must not follow symlinks on the generating host
    # (for example macOS /var -> /private/var).
    project = project.expanduser().absolute()
    home = home.expanduser().absolute()
    uv = str(Path(uv).expanduser().absolute())
    # WorkingDirectory is a raw path, not a quoted word. A trailing slash also
    # keeps a final space or backslash from being stripped/treated as continuation.
    working_directory = str(project).rstrip("/").replace("%", "%%") + "/"
    return f"""[Unit]
Description=Auto-app job collection and filtering
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory={working_directory}
Environment={_quote(f'AUTO_APP_HOME={home}')}
ExecStart=:{_quote(uv)} run --project {_quote(str(project))} auto-app serve run
Restart=on-failure
RestartSec=30
KillMode=control-group
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
"""
