"""Exercise the standalone ZIP builder without accounts or application data."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_dot_plugin.py"
APP_ID = "asdk_app_0123456789abcdef0123456789abcdef"


def build(output, *arguments, script=SCRIPT):
    return subprocess.run(
        [sys.executable, "-S", str(script), "--output", str(output), *arguments],
        cwd=output.parent,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_bound_package_resolves_registered_app_and_embedded_skill(tmp_path):
    output = tmp_path / "bound.zip"
    result = build(output, "--app-id", APP_ID)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as package:
        assert set(package.namelist()) == {
            "job-search/plugin.json",
            "job-search/.app.json",
            "job-search/skills/job-search/SKILL.md",
        }
        manifest = json.loads(package.read("job-search/plugin.json"))
        assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        assert manifest["name"] == "job-search"
        mapping_path = manifest["extensions"]["com.openai"]["apps"]
        assert mapping_path == "./.app.json"
        apps = json.loads(package.read("job-search/" + mapping_path.removeprefix("./")))
        assert apps == {"apps": {"job-search": {"id": APP_ID, "required": True}}}
        assert package.read("job-search/skills/job-search/SKILL.md").decode().startswith("---\n")
        assert "mcpServers" not in manifest
        assert "hooks" not in manifest.get("extensions", {}).get("com.openai", {})


def test_skills_only_package_does_not_claim_an_app_connection(tmp_path):
    output = tmp_path / "skills.zip"
    result = build(output, "--skills-only")
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as package:
        assert set(package.namelist()) == {
            "job-search/plugin.json",
            "job-search/skills/job-search/SKILL.md",
        }
        manifest = json.loads(package.read("job-search/plugin.json"))
        assert "apps" not in manifest.get("extensions", {}).get("com.openai", {})
    assert "tools" in result.stdout.lower()


@pytest.mark.parametrize("arguments", [
    [],
    ["--skills-only", "--app-id", APP_ID],
    ["--app-id", "plugin_" + APP_ID],
    ["--app-id", "asdk_app_"],
    ["--app-id", "asdk_app_valid\nother"],
])
def test_invalid_or_ambiguous_connection_never_creates_archive(tmp_path, arguments):
    output = tmp_path / "invalid.zip"
    result = build(output, *arguments)
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert not output.exists()


def test_builder_allowlist_excludes_runtime_data_and_desktop_config(tmp_path):
    checkout = tmp_path / "checkout"
    script = checkout / "scripts" / "build_dot_plugin.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    source = checkout / "plugins" / "job-search"
    shutil.copytree(ROOT / "plugins" / "job-search", source)
    for relative in [".env", "data/auto_app.db", "profile/private.txt", ".app.json", "mcp.json", ".mcp.json"]:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE_SENTINEL_DO_NOT_PACKAGE", encoding="utf-8")
    output = tmp_path / "clean.zip"
    result = build(output, "--app-id", APP_ID, script=script)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as package:
        assert set(package.namelist()) == {
            "job-search/plugin.json",
            "job-search/.app.json",
            "job-search/skills/job-search/SKILL.md",
        }
        assert all(b"PRIVATE_SENTINEL" not in package.read(name) for name in package.namelist())


def test_existing_output_is_not_overwritten(tmp_path):
    output = tmp_path / "existing.zip"
    output.write_bytes(b"preserve prior artifact")
    result = build(output, "--skills-only")
    assert result.returncode != 0
    assert output.read_bytes() == b"preserve prior artifact"
