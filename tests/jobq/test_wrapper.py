import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from tests.jobq.test_cli import home  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def layout(tmp_path, runtime):
    base = tmp_path / "job search base"
    deploy = base / "auto-application" / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy2(ROOT / "deploy" / "jobq", deploy / "jobq")
    shim = base / "auto-application" / ".venv" / "bin" / "python"
    shim.parent.mkdir(parents=True)
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    (base / "runtime").symlink_to(runtime)
    return deploy / "jobq"


def test_wrapper_forces_the_runtime_home_and_isolated_python(home, tmp_path):  # noqa: F811
    runtime, ids = home
    evil = tmp_path / "evil" / "auto_app"
    evil.mkdir(parents=True)
    (evil / "__init__.py").write_text("raise SystemExit('shadowed')\n")
    env = {"PATH": os.environ["PATH"], "AUTO_APP_HOME": str(tmp_path / "elsewhere"),
           "PYTHONPATH": str(evil.parent)}
    result = subprocess.run([str(layout(tmp_path, runtime)), "new"], env=env, capture_output=True,
                            text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert {j["job_id"] for j in json.loads(result.stdout)["jobs"]} == \
        {ids["Backend Engineer"], ids["Data Engineer"], ids["ML Engineer"]}


def test_wrapper_is_executable_and_keeps_usage_errors(home, tmp_path):  # noqa: F811
    runtime, _ = home
    assert os.stat(ROOT / "deploy" / "jobq").st_mode & stat.S_IXUSR
    result = subprocess.run([str(layout(tmp_path, runtime)), "new", "--db", "x"],
                            env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert result.returncode == 2


def test_wrapper_starts_python_in_the_runtime_and_passes_arguments_through_untouched(home, tmp_path):  # noqa: F811
    runtime, _ = home
    wrapper = layout(tmp_path, runtime)
    # A stand-in interpreter that reports how the wrapper started it: its directory, AUTO_APP_HOME and arguments.
    (wrapper.parents[1] / ".venv" / "bin" / "python").write_text(
        '#!/bin/sh\npwd -P\nprintf "%s\\n" "$AUTO_APP_HOME" "$@"\n')
    caller = tmp_path / "caller"  # not the runtime: tmp_path is the runtime in this fixture
    caller.mkdir()
    args = ["want", "7", "--reason", "two words; $HOME `id` 'quoted' * ?"]
    result = subprocess.run([str(wrapper), *args], env={"PATH": os.environ["PATH"]}, capture_output=True,
                            text=True, cwd=caller)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(runtime.resolve()),
                                          str((tmp_path / "job search base").resolve() / "runtime"),
                                          "-I", "-m", "auto_app.jobq.cli", *args]
