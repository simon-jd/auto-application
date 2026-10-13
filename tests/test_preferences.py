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

    assert Preferences(exclude_countries=("Canada",)).evaluate([location])["excluded"] is excluded
