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


def test_company_groups_are_normalized_like_company_names(tmp_path):
    path = tmp_path / 'handoff.json'
    path.write_text(json.dumps({'company_limit': 2, 'company_groups': {
        'Amazon': ['Amazon Web Services', 'AWS', 'Amazon.com, Inc.'],
        'Palantir': ['Palantir', 'Palantir Technologies Inc.']}}))
    guard = load_guard(path)
    assert guard.company_limit == 2
    assert guard.company_keys() == {'amazon web services': 'amazon', 'aws': 'amazon', 'amazon com': 'amazon',
                                    'palantir technologies': 'palantir'}  # a group's own name is not listed
    assert load_guard(tmp_path / 'missing.json').company_keys() == {}


@pytest.mark.parametrize('groups, message', [
    (['amazon'], 'company_groups must map'),
    ({'amazon': 'aws'}, 'company_groups must map'),
    ({'amazon': []}, 'company_groups must map'),
    ({'amazon': [5]}, 'company_groups must map'),
    ({'amazon': ['x' * 201]}, 'company_groups must map'),
    ({'amazon': ['aws'], 'Amazon, Inc.': ['amazon science']}, 'repeated group'),
    ({'Inc.': ['aws']}, 'empty or repeated group'),
    ({'amazon': ['LLC']}, 'empty name'),
    ({'amazon': ['aws'], 'aws inc': ['amazon web services']}, 'is a group and also a name'),
    ({'amazon': ['twitch'], 'google': ['Twitch']}, 'in two company groups'),
])
def test_ambiguous_or_malformed_company_groups_are_rejected(tmp_path, groups, message):
    path = tmp_path / 'handoff.json'
    path.write_text(json.dumps({'company_groups': groups}))
    with pytest.raises(ValueError, match=message):
        load_guard(path)


def test_company_groups_have_a_size_limit(tmp_path):
    path = tmp_path / 'handoff.json'
    path.write_text(json.dumps({'company_groups': {f'company {i}': [f'other {i}'] for i in range(501)}}))
    with pytest.raises(ValueError, match='at most 500 groups'):
        load_guard(path)


def test_guard_timezone_is_an_iana_zone(tmp_path):
    path = tmp_path / 'handoff.json'
    path.write_text(json.dumps({'timezone': 'America/Los_Angeles'}))
    assert load_guard(path).timezone == 'America/Los_Angeles'
    for bad in ('Mars/Base', '', 5, '../etc/passwd'):
        path.write_text(json.dumps({'timezone': bad}))
        with pytest.raises(ValueError, match='timezone'):
            load_guard(path)
