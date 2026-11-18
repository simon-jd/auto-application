"""Handoff: the list of held forms waiting for the user's go, and token-free reads of the published apply-kit packages.

The apply-kit folder is the runtime's sibling (job-search/apply-kit next to job-search/runtime), so each test puts its
runtime one level down in its own tmp_path, keeping its apply-kit/ apart from every other test's.
"""

import json
import os
from datetime import timedelta

import pytest
from sqlalchemy import insert, select
from typer.testing import CliRunner

from auto_app.core.db import tables as T
from tests.handoff.test_service import (  # noqa: F401  (env is a fixture)
    NOW,
    env,
    service,
)
from tests.imported_jobs import make_store

RESUME = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3 fictional resume\n%%EOF\n"
LETTER = b"%PDF-1.7\n fictional letter\n%%EOF\n"
ANSWERS = "## Why us?\nA fictional answer, with a non-ASCII character: é.\n"


@pytest.fixture
def tmp_path(tmp_path):
    """The runtime is tmp_path/runtime, so its sibling apply-kit/ belongs to this test alone."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return runtime


@pytest.fixture
def kit(env):  # noqa: F811
    """A published package for Backend Engineer, the index and the two guides, as apply_kit.py lays them out."""
    home, ids = env
    root = home.parent / "apply-kit"
    job = ids["Backend Engineer"]
    package = root / "jobs" / str(job)
    package.mkdir(parents=True)
    manifest = {"job_id": job, "company": "Acme", "title": "Backend Engineer",
                "apply_url": "https://job-boards.greenhouse.io/acme/jobs/101", "files": {}}
    files = {"job.json": json.dumps(manifest).encode(), "resume.pdf": RESUME, "cover_letter.pdf": LETTER,
             "answers.md": ANSWERS.encode(), "fields.json": b'{"fields": {"first_name": "Alex"}}',
             "jd.txt": b"Build fictional backends.\n"}
    for name, data in files.items():
        (package / name).write_bytes(data)
    index = {"jobs": [{"job_id": job, "company": "Acme", "title": "Backend Engineer",
                       "published_at": "2026-10-12T11:00:00+00:00", "has_message": False}]}
    (root / "jobs" / "index.json").write_text(json.dumps(index))
    (root / "answers.md").write_text("# Cheat sheet (fictional)\n")
    (root / "README.md").write_text("# Apply kit (fictional)\n")
    return home, ids, root, job, files, index


# ---- held forms ------------------------------------------------------------------------------------------------

def test_held_lists_filled_forms_waiting_for_the_users_go_oldest_first(env):  # noqa: F811
    home, ids = env
    s = service(home)
    assert s.held_applications(now=NOW) == {"items": []}
    backend, data, ml = ids["Backend Engineer"], ids["Data Engineer"], ids["ML Engineer"]
    s.hold(s.claim(data, "agent-a", now=NOW)["token"], now=NOW + timedelta(minutes=5))
    s.hold(s.claim(backend, "agent-b", now=NOW)["token"], now=NOW + timedelta(minutes=10))
    s.claim(ml, "agent-a", now=NOW)  # claimed but not held: the form is not filled yet
    items = s.held_applications(now=NOW + timedelta(hours=1))["items"]
    assert [i["job_id"] for i in items] == [data, backend]
    assert items[0] == {"job_id": data, "title": "Data Engineer", "company": "Acme",
                        "apply_url": "https://job-boards.greenhouse.io/acme/jobs/102", "agent": "agent-a",
                        "lease_until": items[0]["lease_until"], "held_since": items[0]["held_since"]}
    assert items[0]["lease_until"].startswith("2026-10-13T12:05") and items[0]["held_since"].startswith("2026-10-12T12:05")
    assert items[1]["agent"] == "agent-b" and items[1]["held_since"].startswith("2026-10-12T12:10")


def test_held_breaks_ties_by_job_number(env):  # noqa: F811
    home, ids = env
    s = service(home)
    for title in ("ML Engineer", "Backend Engineer"):
        s.hold(s.claim(ids[title], "agent-a", now=NOW)["token"], now=NOW)
    assert [i["job_id"] for i in s.held_applications(now=NOW)["items"]] == \
        sorted([ids["ML Engineer"], ids["Backend Engineer"]])


def test_a_hold_leaves_the_list_once_it_ends_or_is_used(env):  # noqa: F811
    home, ids = env
    s = service(home)
    ended, used = ids["Backend Engineer"], ids["Data Engineer"]
    s.hold(s.claim(ended, "agent-a", now=NOW)["token"], now=NOW)
    token = s.claim(used, "agent-a", now=NOW)["token"]
    s.hold(token, now=NOW)
    assert [i["job_id"] for i in s.held_applications(now=NOW)["items"]] == sorted([ended, used])
    s.record_attempt(token, "submitted", "the user said submit", now=NOW + timedelta(hours=1))
    assert [i["job_id"] for i in s.held_applications(now=NOW + timedelta(hours=1))["items"]] == [ended]
    assert s.held_applications(now=NOW + timedelta(hours=25))["items"] == []  # the 24-hour hold is over


def rows(home):
    store = make_store(home)
    try:
        with store.engine.connect() as conn:
            return [[dict(r) for r in conn.execute(select(table)).mappings()]
                    for table in (T.applications, T.handoff_claims)]
    finally:
        store.engine.dispose()


def test_held_writes_nothing_and_follows_a_pick_up_by_the_same_agent(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    s.hold(s.claim(job, "agent-a", now=NOW)["token"], now=NOW)
    before = rows(home)
    s.held_applications(now=NOW)
    assert rows(home) == before
    # The same agent picking its held job up again keeps it held, with held_since moved to that moment.
    s.claim(job, "agent-a", now=NOW + timedelta(hours=2))
    (item,) = s.held_applications(now=NOW + timedelta(hours=2))["items"]
    assert item["held_since"].startswith("2026-10-12T14:00") and item["lease_until"].startswith("2026-10-13T12:00")


def test_releasing_a_held_form_puts_it_back_in_the_queue_with_no_hold_left(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "agent-a", now=NOW)["token"]
    s.hold(token, now=NOW + timedelta(minutes=5))
    (app,), _ = rows(home)
    assert (app["status"], app["note"]) == ("ready", "form filled; waiting for the user's go")
    assert s.release(token, now=NOW + timedelta(minutes=10)) == {"job_id": job, "released": True}
    (app,), claims = rows(home)
    assert (app["status"], app["note"], claims) == ("queued", None, [])
    assert app["updated_at"].isoformat().startswith("2026-10-12T12:10")
    assert s.held_applications(now=NOW + timedelta(minutes=10)) == {"items": []}
    assert job in [i["job_id"] for i in s.queue(now=NOW + timedelta(minutes=10))["items"]]
    # Any agent can claim it afresh: it is not a resumed hold.
    assert s.claim(job, "agent-b", now=NOW + timedelta(minutes=15))["resumed"] is False


def test_releasing_a_resumed_hold_with_its_fresh_token_also_clears_it(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    s.hold(s.claim(job, "agent-a", now=NOW)["token"], now=NOW)
    again = s.claim(job, "agent-a", now=NOW + timedelta(hours=1))
    assert again["resumed"] is True
    s.release(again["token"], now=NOW + timedelta(hours=1))
    (app,), claims = rows(home)
    assert (app["status"], app["note"], claims) == ("queued", None, [])


def set_note(home, job, note):
    from sqlalchemy import update

    store = make_store(home)
    try:
        with store.engine.begin() as conn:
            conn.execute(update(T.applications).where(T.applications.c.job_id == job).values(note=note))
    finally:
        store.engine.dispose()


def test_releasing_a_hold_keeps_a_note_that_is_not_the_holds_own(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "agent-a", now=NOW)["token"]
    s.hold(token, now=NOW)
    set_note(home, job, "owner note")  # still `ready`, but someone else's note
    s.release(token, now=NOW + timedelta(minutes=10))
    (app,), claims = rows(home)
    assert (app["status"], app["note"], claims) == ("queued", "owner note", [])


def test_a_fresh_claim_after_an_expired_hold_drops_the_holds_note(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    s.hold(s.claim(job, "agent-a", now=NOW)["token"], now=NOW)
    later = NOW + timedelta(hours=25)  # the 24-hour hold has ended without a release
    (app,), _ = rows(home)
    assert (app["status"], app["note"]) == ("ready", "form filled; waiting for the user's go")
    assert s.claim(job, "agent-b", now=later)["resumed"] is False
    (app,), _ = rows(home)
    assert (app["status"], app["note"]) == ("queued", None)
    # Any other note survives a fresh claim.
    s.release(s.claim(ids["Data Engineer"], "agent-a", now=later)["token"], now=later)
    set_note(home, ids["Data Engineer"], "owner note")
    s.claim(ids["Data Engineer"], "agent-b", now=later)
    assert [a["note"] for a in rows(home)[0] if a["job_id"] == ids["Data Engineer"]] == ["owner note"]


def test_releasing_a_plain_claim_leaves_the_application_unchanged(env):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "agent-a", now=NOW)["token"]
    (before,), _ = rows(home)
    assert before["status"] == "queued"
    assert s.release(token, now=NOW + timedelta(minutes=10)) == {"job_id": job, "released": True}
    (after,), claims = rows(home)
    assert after == before and claims == []
    with pytest.raises(HandoffError, match="unknown or released"):  # the token is used up
        s.release(token, now=NOW + timedelta(minutes=11))


def test_held_reads_only_each_jobs_latest_application(env):  # noqa: F811
    """A job can have several application rows (apply/queue.py inserts them too); only the newest one counts."""
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    s.hold(s.claim(job, "agent-a", now=NOW)["token"], now=NOW)  # the claim stays live for the 24-hour hold
    assert [i["job_id"] for i in s.held_applications(now=NOW)["items"]] == [job]

    def add_application(status, when):
        store = make_store(home)
        try:
            with store.engine.begin() as conn:
                conn.execute(insert(T.applications).values(
                    job_id=job, platform="greenhouse", apply_url="https://job-boards.greenhouse.io/acme/jobs/101",
                    status=status, chain=["manual"], chain_pos=0, created_at=when, updated_at=when))
        finally:
            store.engine.dispose()

    # A newer row that is not held: the older held row no longer counts, although the claim is still live.
    add_application("queued", NOW + timedelta(minutes=5))
    assert s.held_applications(now=NOW + timedelta(minutes=10)) == {"items": []}
    # An even newer held row: the job is listed again, held since that row's last update.
    add_application("ready", NOW + timedelta(minutes=20))
    (item,) = s.held_applications(now=NOW + timedelta(minutes=30))["items"]
    assert item["job_id"] == job and item["agent"] == "agent-a"
    assert item["held_since"].startswith("2026-10-12T12:20")


# ---- the apply-kit packages, read with no token ----------------------------------------------------------------

def test_the_apply_kit_is_the_runtimes_sibling(env):  # noqa: F811
    from auto_app.handoff.service import APPLY_KIT_DIR

    home, _ = env
    assert APPLY_KIT_DIR == "apply-kit" and service(home).kit_dir == home.resolve().parent / "apply-kit"


def test_kit_index_and_files_need_no_claim(kit):
    from auto_app.handoff.service import KIT_FILES

    home, _, _, job, files, index = kit
    s = service(home)
    assert s.kit_index() == index
    for name, data in files.items():
        assert s.kit_file(job, name) == data
    assert set(files) | {"message.txt"} == set(KIT_FILES)


def test_kit_index_is_empty_before_anything_is_published(env):  # noqa: F811
    home, _ = env
    assert service(home).kit_index() == {"jobs": []}
    (home.parent / "apply-kit" / "jobs").mkdir(parents=True)
    assert service(home).kit_index() == {"jobs": []}


@pytest.mark.parametrize("body", [b"{not json", b"[]", b'{"jobs": {}}', b"\xff\xfe"])
def test_a_broken_index_is_an_error(kit, body):
    from auto_app.handoff.service import HandoffError

    home, _, root, *_ = kit
    (root / "jobs" / "index.json").write_bytes(body)
    with pytest.raises(HandoffError, match="index.json"):
        service(home).kit_index()


def test_a_missing_file_or_package_says_it_is_not_published_yet(kit):
    from auto_app.handoff.service import HandoffError

    home, ids, root, job, *_ = kit
    s = service(home)
    with pytest.raises(HandoffError, match=rf"^job #{job} has no message.txt in its package \(not published yet\?\)$"):
        s.kit_file(job, "message.txt")
    other = ids["Data Engineer"]
    with pytest.raises(HandoffError, match=rf"job #{other} has no resume.pdf in its package"):
        s.kit_file(other, "resume.pdf")


def test_without_an_apply_kit_folder_every_read_says_so(env):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    with pytest.raises(HandoffError, match="not published yet"):
        s.kit_file(ids["Backend Engineer"], "job.json")
    with pytest.raises(HandoffError, match="answers.md"):
        s.kit_guide("cheatsheet")


@pytest.mark.parametrize("name", ["../job.json", "profile.yaml", "index.json", "resume.docx", "", None])
def test_only_the_package_files_can_be_read(kit, name):
    from auto_app.handoff.service import HandoffError

    home, _, _, job, *_ = kit
    with pytest.raises(HandoffError, match="name must be one of"):
        service(home).kit_file(job, name)


@pytest.mark.parametrize("job_id", [0, -1, "1", 1.0, True])
def test_job_numbers_are_positive_integers(kit, job_id):
    from auto_app.handoff.service import HandoffError

    home, *_ = kit
    with pytest.raises(HandoffError, match="job_id"):
        service(home).kit_file(job_id, "job.json")


def test_symlinks_are_refused_at_every_level(kit, tmp_path):
    from auto_app.handoff.service import HandoffError

    home, ids, root, job, *_ = kit
    s = service(home)
    secret = tmp_path.parent / "outside.txt"
    secret.write_text("not part of any package")
    package = root / "jobs" / str(job)
    (package / "message.txt").symlink_to(secret)  # a file that points outside
    with pytest.raises(HandoffError, match="no symlinks"):
        s.kit_file(job, "message.txt")
    (root / "jobs" / str(ids["Data Engineer"])).symlink_to(package, target_is_directory=True)  # a package folder
    with pytest.raises(HandoffError, match="no symlinks"):
        s.kit_file(ids["Data Engineer"], "resume.pdf")
    (root / "README.md").unlink()
    (root / "README.md").symlink_to(secret)  # a guide
    with pytest.raises(HandoffError, match="no symlinks"):
        s.kit_guide("readme")
    moved = root.with_name("apply-kit-real")
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)  # the apply-kit folder itself
    with pytest.raises(HandoffError, match="no symlinks"):
        s.kit_index()
    with pytest.raises(HandoffError, match="no symlinks"):
        s.kit_file(job, "resume.pdf")


def test_a_file_over_10_mib_is_refused(kit):
    from auto_app.handoff.service import HandoffError

    home, _, root, job, *_ = kit
    with open(root / "jobs" / str(job) / "resume.pdf", "r+b") as handle:
        handle.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(HandoffError, match="too large"):
        service(home).kit_file(job, "resume.pdf")


def test_a_directory_in_place_of_a_file_is_refused(kit):
    from auto_app.handoff.service import HandoffError

    home, _, root, job, *_ = kit
    os.mkdir(root / "jobs" / str(job) / "message.txt")
    with pytest.raises(HandoffError, match="regular file"):
        service(home).kit_file(job, "message.txt")


def test_the_two_guides(kit):
    from auto_app.handoff.service import HandoffError

    home, _, root, *_ = kit
    s = service(home)
    assert s.kit_guide("cheatsheet") == b"# Cheat sheet (fictional)\n"
    assert s.kit_guide() == s.kit_guide("cheatsheet")
    assert s.kit_guide("readme") == b"# Apply kit (fictional)\n"
    with pytest.raises(HandoffError, match="cheatsheet or readme"):
        s.kit_guide("answers.md")
    (root / "README.md").unlink()
    with pytest.raises(HandoffError, match="README.md"):
        s.kit_guide("readme")


# ---- the owner's CLI -------------------------------------------------------------------------------------------

def test_owner_cli_lists_held_forms(env):  # noqa: F811
    from auto_app.cli import app

    home, ids = env
    s = service(home)
    s.hold(s.claim(ids["ML Engineer"], "my-agent")["token"])
    result = CliRunner().invoke(app, ["handoff", "held"], env={"AUTO_APP_HOME": str(home)})
    assert result.exit_code == 0, result.output
    assert [(i["job_id"], i["agent"]) for i in json.loads(result.output)["items"]] == [(ids["ML Engineer"], "my-agent")]
