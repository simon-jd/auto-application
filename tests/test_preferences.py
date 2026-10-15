"""Hand-picked geography cases catch Canadian misses and California false positives."""

import pytest


@pytest.mark.parametrize("location,excluded", [
    ("Remote in Canada", True), ("Canada", True), ("Toronto, ON", True),
    ("Vancouver, BC", True), ("Montréal, QC", True), ("Québec, Canada", True),
    ("Ottawa, Ontario", True), ("Calgary, AB", True), ("Winnipeg, MB", True),
    ("Halifax, NS", True), ("St. John's, NL", True), ("Remote - British Columbia", True),
    ("San Jose, CA", False), ("Ontario, CA", False), ("Vancouver, WA", False),
    ("Remote in USA", False), ("London, UK", False), ("Remote", False),
    ("Work on platform", False), ("Seattle, WA", False),
    ("New Brunswick, NJ", False), ("New Brunswick, New Jersey", False),
    ("Ontario, OR", False), ("Ontario, Oregon", False), ("Alberta, VA", False),
    ("New Brunswick, NJ; Canada", True),
    ("Ontario, U.S.", False), ("New Brunswick, U.S.A.", False),
    ("Toronto, ON, CA", True), ("Toronto, ON / Boston, MA", True),
])
def test_canada_matching(location, excluded):
    from auto_app.preferences import Preferences

    # Detection only: whether a string names a Canadian place (evaluate also weighs US places).
    assert bool(Preferences(exclude_countries=("Canada",)).countries_for(location)) is excluded


@pytest.mark.parametrize("locations,countries,excluded", [
    (["Remote"], ["CA"], True), (["Remote"], ["US", "CA"], False), (["Remote"], ["US"], False),
    (["San Jose, CA"], [], False),  # a location's CA is California, never the country code
    (["San Jose, CA"], ["CA"], False),  # ...and a US place keeps the job even beside a Canadian code
])
def test_structured_country_codes_are_distinct_from_california(locations, countries, excluded):
    from auto_app.preferences import Preferences

    result = Preferences(exclude_countries=("Canada",)).evaluate(locations, countries=countries)
    assert result["excluded"] is excluded
    assert result["matched_locations"] == []


@pytest.mark.parametrize("location,excluded", [
    ("Toronto, Ontario; Seattle, WA", True),
    ("Vancouver, British Columbia / Seattle, WA", True),
    ("Seattle, WA / Vancouver, British Columbia", True),
    ("New Brunswick, NJ / Toronto, Ontario", True),
    ("New Brunswick, NJ; Ontario, CA", False),
    ("Ontario, CA / Vancouver, WA", False),
    ("Alberta, VA; Seattle, WA", False),
])
def test_us_location_disambiguates_only_its_own_location_segment(location, excluded):
    from auto_app.preferences import Preferences

    preferences = Preferences(exclude_countries=("Canada",))
    assert bool(preferences.countries_for(location)) is excluded
    # Every case also lists a US place, so the job itself stays visible.
    assert preferences.evaluate([location]) == {"excluded": False, "reasons": [], "matched_locations": []}


@pytest.mark.parametrize("locations,excluded", [
    (["Seattle, WA", "Vancouver, BC"], False), (["Vancouver, BC", "Toronto, ON"], True),
    (["Toronto, ON, CA"], True), (["Remote - US or Canada"], False),
])
def test_a_job_with_any_us_location_is_kept(locations, excluded):
    from auto_app.preferences import Preferences

    assert Preferences(exclude_countries=("Canada",)).evaluate(locations)["excluded"] is excluded


def test_excluding_the_united_states_itself_disables_the_us_exception():
    from auto_app.preferences import Preferences

    result = Preferences(exclude_countries=("United States", "Canada")).evaluate(["Boston, MA", "Toronto, ON"])
    assert result["excluded"] is True
