import json

import pytest

from auto_app.handoff.guard import Guard, load_guard


def test_exact_guard_defaults_and_partial_overrides(tmp_path):
    path = tmp_path / 'handoff.json'
    assert load_guard(path) == Guard(20, 30, 3, 30)
    path.write_text(json.dumps({'daily_submissions': 5}))
    assert load_guard(path) == Guard(5, 30, 3, 30)


@pytest.mark.parametrize('data', [[], {'daily_submissions': True}, {'company_limit': 0},
                                  {'lease_minutes': 1001}, {'extra': 'hidden'}, {'company_window_days': '30'}])
def test_guard_invalid_data_is_rejected(tmp_path, data):
    path = tmp_path / 'handoff.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError) as exc:
        load_guard(path)
    assert 'hidden' not in str(exc.value)


def test_guard_timezone_is_an_iana_zone(tmp_path):
    path = tmp_path / 'handoff.json'
    path.write_text(json.dumps({'timezone': 'America/Los_Angeles'}))
    assert load_guard(path).timezone == 'America/Los_Angeles'
    for bad in ('Mars/Base', '', 5, '../etc/passwd'):
        path.write_text(json.dumps({'timezone': bad}))
        with pytest.raises(ValueError, match='timezone'):
            load_guard(path)
