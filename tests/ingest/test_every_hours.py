"""A feed's every_hours: long-tail employer boards are read less often than the core feeds (2026-11-18)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from auto_app.ingest.runner import load_feed_config, run_config
from tests.imported_jobs import make_store

T0 = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)


def config(tmp_path, **extra):
    (tmp_path / "feed.json").write_text(json.dumps([]))
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {
        "slow": {"kind": "repository", "location": "feed.json", **extra},
        "fast": {"kind": "repository", "location": "feed.json"}}}))
    return path


def test_a_feed_with_every_hours_is_read_once_per_interval(tmp_path):
    store = make_store(tmp_path)
    path = config(tmp_path, every_hours=6)
    try:
        assert run_config(store, path, now=T0)["sources"]["slow"]["status"] == "ok"
        later = run_config(store, path, now=T0 + timedelta(hours=1))
        assert later["sources"]["slow"]["status"] == "deferred" and "every 6 hours" in later["sources"]["slow"]["reason"]
        assert later["sources"]["fast"]["status"] == "ok" and later["status"] == "ok"
        assert run_config(store, path, now=T0 + timedelta(hours=6, minutes=1))["sources"]["slow"]["status"] == "ok"
    finally:
        store.engine.dispose()


def test_every_hours_is_kept_in_the_loaded_config(tmp_path):
    assert load_feed_config(config(tmp_path, every_hours=0.5))["slow"]["every_hours"] == 0.5
    assert "every_hours" not in load_feed_config(config(tmp_path))["slow"]


@pytest.mark.parametrize("value", [0, -1, 169, True, "6", None])
def test_every_hours_must_be_a_positive_number_of_hours_up_to_a_week(tmp_path, value):
    with pytest.raises(ValueError, match="every_hours"):
        load_feed_config(config(tmp_path, every_hours=value))


def test_interval_feeds_share_a_per_round_budget_most_overdue_first(tmp_path, monkeypatch):
    from auto_app.ingest import runner

    monkeypatch.setattr(runner, "EVERY_PER_ROUND", 2)
    (tmp_path / "feed.json").write_text(json.dumps([]))
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {
        name: {"kind": "repository", "location": "feed.json", "every_hours": 6} for name in ("a", "b", "c")}}))
    store = make_store(tmp_path)
    try:
        first = run_config(store, path, now=T0)["sources"]
        assert sorted(n for n, r in first.items() if r["status"] == "ok") == ["a", "b"]
        assert first["c"]["status"] == "deferred" and "turn" in first["c"]["reason"]
        second = run_config(store, path, now=T0 + timedelta(minutes=30))["sources"]
        assert [n for n, r in second.items() if r["status"] == "ok"] == ["c"]  # never read goes first
    finally:
        store.engine.dispose()
