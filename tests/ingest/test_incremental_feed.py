"""Incremental feeds: Grok's daily files of new listings, read only once each and never closing what they omit."""

import json

import pytest
import yaml

from auto_app.ingest.runner import load_feed_config, run_config
from tests.ingest.test_runner import entries, row, store  # noqa: F401  (store is a fixture)


def config(tmp_path, **definition):
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {"grok-linkedin-daily": {
        "kind": "repository", "location": "daily/*.json", "incremental": True, **definition}}}))
    (tmp_path / "daily").mkdir(exist_ok=True)
    return path


def day(tmp_path, name, rows):
    (tmp_path / "daily" / name).write_text(json.dumps(rows))


def test_each_daily_file_is_read_once_and_absence_closes_nothing(store, tmp_path):  # noqa: F811
    path = config(tmp_path)
    day(tmp_path, "2026-11-19.json", [row("one"), row("two")])
    first = run_config(store, path)["sources"]["grok-linkedin-daily"]
    assert first["status"] == "ok" and first["created"] == 2 and first["files_read"] == 1
    again = run_config(store, path)["sources"]["grok-linkedin-daily"]
    assert again["status"] == "ok" and again["files_read"] == 0 and again.get("created", 0) == 0
    day(tmp_path, "2026-11-20.json", [row("three")])  # "one" and "two" are not in today's file
    third = run_config(store, path)["sources"]["grok-linkedin-daily"]
    assert third["files_read"] == 1 and third["created"] == 1 and third.get("missing", 0) == 0
    assert all(e["state"] != "missing" for e in entries(store).values() if "state" in e)
    assert store.source_health("grok-linkedin-daily")["ingest"]["incremental"] is True


def test_a_rewritten_daily_file_is_read_again(store, tmp_path):  # noqa: F811
    path = config(tmp_path)
    day(tmp_path, "2026-11-19.json", [row("one")])
    run_config(store, path)
    day(tmp_path, "2026-11-19.json", [row("one", title="Senior Backend Engineer"), row("two")])
    report = run_config(store, path)["sources"]["grok-linkedin-daily"]
    assert report["files_read"] == 1 and report["created"] == 1 and report["updated"] == 1


def test_no_files_yet_is_fine(store, tmp_path):  # noqa: F811
    report = run_config(store, config(tmp_path))["sources"]["grok-linkedin-daily"]
    assert report["status"] == "ok" and report["files_read"] == 0


@pytest.mark.parametrize("changes,message", [
    ({"location": "https://raw.githubusercontent.com/x/y/main/*.json"}, "local"),
    ({"format": "greenhouse-board", "location": "https://boards-api.greenhouse.io/v1/boards/x/jobs"}, "incremental"),
    ({"incremental": "yes"}, "incremental"),
])
def test_incremental_settings_are_checked(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        load_feed_config(config(tmp_path, **changes))


def test_the_loaded_config_keeps_the_pattern(tmp_path):
    loaded = load_feed_config(config(tmp_path))["grok-linkedin-daily"]
    assert loaded["incremental"] is True and loaded["location"] == str(tmp_path / "daily" / "*.json")
