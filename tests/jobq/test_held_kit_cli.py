"""jobq: the held forms waiting for the user's go (held), and the published apply-kit packages and guides (kit, guide),
read with no claim token, also through sudo. `kit ID FILE` and `guide` print the file itself, not JSON."""

import json

import pytest

from auto_app.jobq.cli import main, parser
from tests.jobq.test_cli import home, run  # noqa: F401  (home is a fixture)

RESUME = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3 fictional resume\x00\n%%EOF\n"
CHEATSHEET = "# Cheat sheet (fictional)\nAnswer from answers.md only. é\n"


@pytest.fixture
def tmp_path(tmp_path):
    """The runtime is tmp_path/runtime, so its sibling apply-kit/ belongs to this test alone."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return runtime


@pytest.fixture
def kit(home):  # noqa: F811
    root, ids = home
    kit = root.parent / "apply-kit"
    job = ids["ML Engineer"]
    package = kit / "jobs" / str(job)
    package.mkdir(parents=True)
    manifest = {"job_id": job, "company": "Beta", "title": "ML Engineer", "files": {"resume.pdf": {"bytes": len(RESUME)}}}
    (package / "job.json").write_text(json.dumps(manifest))
    (package / "resume.pdf").write_bytes(RESUME)
    (kit / "jobs" / "index.json").write_text(json.dumps({"jobs": [{"job_id": job, "title": "ML Engineer"}]}))
    (kit / "answers.md").write_text(CHEATSHEET, encoding="utf-8")
    (kit / "README.md").write_text("# Apply kit (fictional)\n")
    return kit, job, manifest


def raw(capsysbinary, *args):
    """The exit status and stdout's bytes."""
    code = main(list(args))
    return code, capsysbinary.readouterr().out


def as_json(capsysbinary, *args):
    code, out = raw(capsysbinary, *args)
    return code, json.loads(out)


# ---- held ------------------------------------------------------------------------------------------------------

def test_held_lists_the_forms_grok_holds(home, capsys):  # noqa: F811
    _, ids = home
    job = ids["ML Engineer"]
    assert run(capsys, "held") == (0, {"items": []})
    run(capsys, "want", str(job), "--reason", "the user picked it (test)")  # only wanted jobs are queued
    token = run(capsys, "claim", str(job))[1]["token"]
    assert run(capsys, "hold", token)[1]["status"] == "ready"
    code, out = run(capsys, "held")
    assert code == 0 and [(i["job_id"], i["agent"], i["title"]) for i in out["items"]] == \
        [(job, "grok-bot", "ML Engineer")]
    assert set(out["items"][0]) == {"job_id", "title", "company", "apply_url", "agent", "lease_until", "held_since"}


# ---- kit and guide ---------------------------------------------------------------------------------------------

def test_kit_prints_the_index_then_a_jobs_manifest(kit, capsysbinary):
    _, job, manifest = kit
    assert as_json(capsysbinary, "kit") == (0, {"jobs": [{"job_id": job, "title": "ML Engineer"}]})
    assert as_json(capsysbinary, "kit", str(job)) == (0, manifest)


def test_kit_id_file_prints_the_raw_bytes(kit, capsysbinary):
    _, job, _ = kit
    assert raw(capsysbinary, "kit", str(job), "resume.pdf") == (0, RESUME)


def test_guide_prints_the_cheat_sheet_or_the_readme(kit, capsysbinary):
    assert raw(capsysbinary, "guide") == (0, CHEATSHEET.encode())
    assert raw(capsysbinary, "guide", "cheatsheet") == (0, CHEATSHEET.encode())
    assert raw(capsysbinary, "guide", "readme") == (0, b"# Apply kit (fictional)\n")


def test_reads_need_no_token_and_work_through_sudo(kit, capsysbinary, monkeypatch):
    _, job, _ = kit
    monkeypatch.setenv("SUDO_USER", "grokbot")
    assert raw(capsysbinary, "kit", str(job), "resume.pdf") == (0, RESUME)
    assert raw(capsysbinary, "guide")[0] == 0
    assert as_json(capsysbinary, "held") == (0, {"items": []})


def test_missing_files_are_json_errors(kit, capsysbinary):
    root, job, _ = kit
    assert as_json(capsysbinary, "kit", str(job), "cover_letter.pdf") == (1, {
        "status": "error", "error": f"job #{job} has no cover_letter.pdf in its package (not published yet?)"})
    assert as_json(capsysbinary, "kit", "999999")[0] == 1
    (root / "README.md").unlink()
    code, out = as_json(capsysbinary, "guide", "readme")
    assert code == 1 and out["status"] == "error" and "README.md" in out["error"]


def test_a_symlinked_file_is_a_json_error(kit, capsysbinary, tmp_path):
    root, job, _ = kit
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("not part of any package")
    (root / "jobs" / str(job) / "message.txt").symlink_to(outside)
    code, out = as_json(capsysbinary, "kit", str(job), "message.txt")
    assert code == 1 and "no symlinks" in out["error"] and "not part of" not in out["error"]


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]"])
def test_a_broken_manifest_is_a_json_error(kit, capsysbinary, body):
    root, job, _ = kit
    (root / "jobs" / str(job) / "job.json").write_bytes(body)
    code, out = as_json(capsysbinary, "kit", str(job))
    assert code == 1 and "job.json" in out["error"]


@pytest.mark.parametrize("args", [
    ["kit", "resume.pdf"],  # the job number comes first
    ["kit", "1", "../profile/profile.yaml"],
    ["kit", "1", "index.json"],
    ["kit", "1", "resume.pdf", "extra"],
    ["kit", "0"],
    ["guide", "answers.md"],
    ["held", "1"],
])
def test_bad_arguments_exit_2(kit, args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2


def test_kit_files_match_the_service():
    from auto_app.handoff.service import KIT_FILES, KIT_GUIDES
    from auto_app.jobq import cli

    assert cli.KIT_FILES == KIT_FILES and cli.GUIDES == tuple(KIT_GUIDES)


def test_help_lists_the_new_commands(capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["--help"])
    text = capsys.readouterr().out
    assert all(f"    {name} " in text for name in ("held", "kit", "guide"))
    with pytest.raises(SystemExit):
        parser().parse_args(["kit", "--help"])
    assert "resume.pdf > ~/kit/" in capsys.readouterr().out
