"""The paged board registry and the paging rule the Eightfold, Jibe, Oracle and SmartRecruiters readers share."""

import pytest

from auto_app.ingest.repository import REPOSITORY_FORMATS
from auto_app.ingest.repository_paged import BOARD_MODULES, feed_options, paged_board, read_pages


def test_every_paged_format_is_a_repository_format_with_its_own_registry_entry():
    assert set(BOARD_MODULES) <= set(REPOSITORY_FORMATS)
    for format in BOARD_MODULES:
        assert paged_board(format).format == format
    assert paged_board("simplify") is None and paged_board("greenhouse-board") is None
    required = {format for format in BOARD_MODULES if paged_board(format).company_required}
    assert required == {"workday-board", "eightfold-board", "oracle-board"}  # their lists never name the employer


def test_options_belong_to_their_formats():
    assert feed_options({"location": "feed.json"}, format="simplify") is None
    with pytest.raises(ValueError, match="country apply only to format eightfold-board or icims-jibe-board or "
                                         "oracle-board or smartrecruiters-board"):
        feed_options({"country": "us"}, format="workday-board")
    with pytest.raises(ValueError, match="filters apply only to format eightfold-board"):
        feed_options({"filters": {"job_family": ["x"]}}, format="oracle-board")


def pages(*answers):
    """fetch_page answering (rows, total) in turn."""
    answers = list(answers)
    return lambda text, page: answers.pop(0)


def rows_of(*ids):
    return [{"id": value} for value in ids]


def test_a_search_is_complete_only_when_it_read_the_largest_total_reported():
    rows, [search] = read_pages(pages((rows_of("1", "2"), 3), (rows_of("3"), 3)), searches=[],
                                page_size=2, max_pages=5, key=lambda row: row["id"])
    assert sorted(rows) == ["1", "2", "3"] and search == {"search_text": "", "total": 3, "read": 3, "pages": 2,
                                                          "reads": 1, "complete": True}


def test_a_read_that_ends_short_is_repeated_once():
    # "0" is posted while page 1 is read: the newest-first pages shift, "2" repeats and "0" is missed.
    # The second read sees the board as it now is, and the search keeps the rows of both reads.
    rows, [search] = read_pages(pages((rows_of("1", "2"), 3), (rows_of("2", "3"), 4),
                                      (rows_of("0", "1"), 4), (rows_of("2", "3"), 4)),
                                searches=["x"], page_size=2, max_pages=5, key=lambda row: row["id"])
    assert sorted(rows) == ["0", "1", "2", "3"]
    assert search == {"search_text": "x", "total": 4, "read": 4, "pages": 4, "reads": 2, "complete": True}
    # A board that keeps changing stays partial after the second read.
    _, [moving] = read_pages(pages((rows_of("1", "2"), 3), (rows_of("2", "3"), 4), (rows_of("0", "1"), 5),
                                   (rows_of("1", "2"), 5), (rows_of("3"), 5)),
                             searches=[], page_size=2, max_pages=5, key=lambda row: row["id"])
    assert moving["reads"] == 2 and moving["read"] == 4 and moving["complete"] is False
    # A read stopped by the page cap is not repeated: raising max_pages is the remedy.
    _, [capped] = read_pages(pages((rows_of("1", "2"), 3)), searches=[], page_size=2, max_pages=1,
                             key=lambda row: row["id"])
    assert capped["reads"] == 1 and capped["complete"] is False


def test_rows_without_a_key_are_kept_for_the_parser_to_report():
    rows, [search] = read_pages(pages(([{"id": "1"}, {"id": None}, "x"], 3),), searches=["a"], page_size=5,
                                max_pages=1, key=lambda row: row.get("id") if isinstance(row, dict) else None)
    assert len(rows) == 3 and search["complete"] is True and search["search_text"] == "a"
