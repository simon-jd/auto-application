"""US-only screening: explicit non-US evidence excludes; US, US-remote and unknown places stay."""

import json

import pytest

from auto_app.screen.locations import classify_location, job_location
from auto_app.screen.rules import JobFacts, ScreeningRules, load_rules, screen

RULES = ScreeningRules()


@pytest.mark.parametrize("text, expected", [
    ("San Jose, CA", "us"),
    ("NYC", "us"),
    ("SF", "us"),
    ("Remote in USA", "us"),
    ("Remote - US", "us"),
    ("US Remote", "us"),
    ("Remote (United States)", "us"),
    ("US, CA, Santa Clara", "us"),
    ("Dublin, CA", "us"),  # a US city named like a foreign capital
    ("Vancouver, WA", "us"),
    ("New Mexico", "us"),
    ("London, UK", "non_us"),
    ("London", "non_us"),
    ("Bengaluru, India", "non_us"),
    ("Toronto, ON, Canada", "non_us"),
    ("Toronto, ON, CA", "non_us"),  # a Canadian province: CA is the country, not California
    ("Ontario, CA", "us"),
    ("New Brunswick, NJ", "us"),
    ("Remote - US or Canada", "us"),
    ("Toronto, ON / Boston, MA", "us"),  # one of the listed places is in the US
    ("Toronto, Ontario; Seattle, WA", "us"),
    ("Vancouver, BC", "non_us"),
    ("Remote in Canada", "non_us"),
    ("Remote - EMEA", "non_us"),
    ("Singapore", "non_us"),
    ("Berlin, Germany", "non_us"),
    ("Remote", "unknown"),
    ("Hybrid", "unknown"),
    ("", "unknown"),
])
def test_classify_location(text, expected):
    assert classify_location(text) == expected


@pytest.mark.parametrize("locations, countries, expected", [
    (("London, UK", "New York, NY"), (), "us"),  # any US location keeps the job
    (("Remote", "London, UK"), (), "non_us"),  # the only explicit place is outside the US
    (("Remote",), (), "unknown"),
    (("Remote",), ("GB",), "non_us"),  # Lever's structured country
    (("Remote",), ("US",), "us"),
    ((), (), "unknown"),
])
def test_job_location_combines_every_location(locations, countries, expected):
    assert job_location(locations, countries) == expected


def facts(title="Software Engineer", **kw):
    return JobFacts(job_id=1, titles=(title,), **kw)


def test_jobs_outside_the_us_are_excluded_and_others_kept():
    abroad = screen(facts(locations=("London, UK",)), RULES)
    assert abroad.outcome == "excluded" and abroad.reasons == ("outside_us",)
    assert abroad.signals["location"] == "non_us"
    assert screen(facts(locations=("London, UK", "Austin, TX")), RULES).outcome == "kept"
    unknown = screen(facts(locations=("Remote",)), RULES)
    assert unknown.outcome == "kept" and unknown.signals["location"] == "unknown"
    assert screen(facts(locations=("Remote",), countries=("GB",)), RULES).reasons == ("outside_us",)


def test_the_us_only_rule_can_be_turned_off(tmp_path):
    path = tmp_path / "screening.json"
    path.write_text(json.dumps({"us_only": False}))
    relaxed = load_rules(path)
    assert relaxed == ScreeningRules(us_only=False) and relaxed.version != RULES.version
    assert screen(facts(locations=("London, UK",)), relaxed).outcome == "kept"
