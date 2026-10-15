import pytest

from auto_app.sources.common.text import parse_salary
from auto_app.sources.parsing import req_id_from_text


@pytest.mark.parametrize("text", [
    "Please include the job id when applying.",
    "PLEASE INCLUDE THE JOB ID WHEN APPLYING.",
    "The requisition ID will be provided later.",
    "Your job identity is confidential.",
])
def test_requisition_parser_does_not_extract_prose(text):
    assert req_id_from_text(text) is None


@pytest.mark.parametrize("text,expected", [
    ("Job ID: JR2026343", "JR2026343"),
    ("job id 2026343", "2026343"),
    ("Requisition number: req-12345", "req-12345"),
    ("Req # AB-1234", "AB-1234"),
    ("Job ID: ABCD", "ABCD"),
    ("Include the job id when applying.\nJob ID: JR2026343", "JR2026343"),
])
def test_requisition_parser_retains_explicit_identifiers(text, expected):
    assert req_id_from_text(text) == expected


@pytest.mark.parametrize("benefits", ["Medical", "medical insurance", "Matching contributions", "Knowledge budget"])
def test_salary_suffix_does_not_consume_following_benefit_words(benefits):
    salary = parse_salary(f"$150,000 - $200,000 {benefits}", "description")
    assert (salary.min, salary.max, salary.period) == (150_000, 200_000, "year")
    assert salary.text == "$150,000 - $200,000"


@pytest.mark.parametrize("text,lo,hi", [
    ("$150k - $200K/year", 150_000, 200_000),
    ("$1.5M - $2 M annually", 1_500_000, 2_000_000),
    ("$30/hr - $35/hr", 30, 35),
])
def test_salary_keeps_real_unit_suffixes(text, lo, hi):
    salary = parse_salary(text, "description")
    assert (salary.min, salary.max) == (lo, hi)
