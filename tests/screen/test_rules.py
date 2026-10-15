"""Deterministic screening: only explicit evidence excludes; sponsorship is a signal."""

import json

import pytest

from auto_app.screen.rules import JobFacts, ScreeningRules, load_rules, screen, years_required

RULES = ScreeningRules()


@pytest.mark.parametrize("description", [
    "With 25 years of industry experience, Acme builds payment systems.",
    "Acme has over 30 years of experience in banking software.",
    "Founded in 1999, we bring 25+ years of experience to our clients.",
])
def test_employer_tenure_is_not_a_requirement(description):
    assert years_required(description) is None


def test_requirements_after_a_tenure_sentence_still_count():
    text = ("With 25 years of industry experience, Acme builds payments. "
            "Requires 5+ years of professional experience.")
    assert years_required(text) == 5


@pytest.mark.parametrize("description", [
    "You have 5+ years of professional experience.",
    "Candidates must have over 5 years of experience.",
    "The ideal applicant has 5 years of experience.",
    "The developer has 5 years of experience.",
])
def test_candidate_experience_is_not_mistaken_for_employer_tenure(description):
    assert years_required(description) == 5


def facts(title, **kw):
    return JobFacts(job_id=1, titles=(title,), **kw)


@pytest.mark.parametrize("title", [
    "Software Engineer, New Grad", "Software Engineer I", "Data Scientist - Entry Level",
    "Site Reliability Engineer", "Internal Tools Engineer", "Machine Learning Engineer (2027 New Grad)",
    "Software Engineer II", "Associate Software Engineer", "Software Engineer - V&V",
    "Software Engineer, New Grad (Summer 2027 start)", "Field Applications Engineer",
])
def test_entry_level_cs_and_ambiguous_titles_are_kept(title):
    assert screen(facts(title), RULES).outcome == "kept", title


@pytest.mark.parametrize("title, reason", [
    ("Software Engineer Intern", "internship"),
    ("SWE Co-op (Spring)", "internship"),
    ("Summer 2027 Software Engineering Internship", "internship"),
    ("Software Engineer (Summer 2027)", "internship"),
    ("Senior Software Engineer", "senior_title"),
    ("Sr. Data Engineer", "senior_title"),
    ("Staff ML Engineer", "senior_title"),
    ("Software Engineer III", "senior_title"),
    ("Software Engineer - III", "senior_title"),
    ("Software Engineer, Staff", "senior_title"),
    ("Engineering Manager, Payments", "senior_title"),
    ("Mechanical Engineer", "not_cs_role"),
    ("Account Executive", "not_cs_role"),
    ("Product Manager", "not_cs_role"),
    ("ASIC Design Engineer", "not_cs_role"),
    ("Process Engineer", "not_cs_role"),
])
def test_explicit_title_evidence_excludes(title, reason):
    result = screen(facts(title), RULES)
    assert result.outcome == "excluded" and reason in result.reasons, (title, result.reasons)


def test_internship_from_employment_type_and_intern_only_feeds():
    assert "internship" in screen(facts("Software Engineer", employment_types=("internship",)), RULES).reasons
    assert "internship" in screen(facts("Software Engineer", sources=("simplify-internships",)), RULES).reasons
    mixed = facts("Software Engineer", sources=("simplify-internships", "simplify-newgrad"))
    assert screen(mixed, RULES).outcome == "kept"


def test_repository_categories_decide_generic_titles():
    assert screen(facts("Engineer", categories=("Hardware",)), RULES).reasons == ("not_cs_role",)
    kept = screen(facts("Engineer", categories=("Software",)), RULES)
    assert kept.outcome == "kept" and kept.signals["role"] == "cs"
    assert screen(facts("Embedded Software Engineer", categories=("Hardware",)), RULES).outcome == "kept"


@pytest.mark.parametrize("labels, description, expected", [
    (("Offers Sponsorship",), "", "offers"),
    (("Does Not Offer Sponsorship",), "", "no"),
    ((), "Visa sponsorship is available for this role.", "offers"),
    ((), "We are unable to sponsor visas for this position.", "no"),
    ((), "Candidates must be authorized to work in the US without sponsorship.", "no"),
    (("Other",), "Build backend services.", "unknown"),
])
def test_sponsorship_is_a_signal_not_an_exclusion(labels, description, expected):
    result = screen(facts("Software Engineer", sponsorship_labels=labels, description=description), RULES)
    assert result.outcome == "kept" and result.signals["sponsorship"] == expected


@pytest.mark.parametrize("labels, description", [
    (("U.S. Citizenship is Required",), ""),
    ((), "Applicants must be a U.S. citizen."),
    ((), "This role requires U.S. citizenship."),
    ((), "Requires an active Secret clearance."),
    ((), "Security clearance is required for this role."),
    ((), "Candidates must be eligible for a security clearance."),
    ((), "Candidates must meet ITAR requirements."),
    ((), "Must have the ability to obtain a security clearance."),
    ((), "This position requires access to export-controlled data; candidates must be U.S. persons."),
])
def test_citizenship_or_clearance_requirements_exclude(labels, description):
    result = screen(facts("Software Engineer", sponsorship_labels=labels, description=description), RULES)
    assert result.reasons == ("citizenship_required",)
    assert result.signals["sponsorship"] == "citizenship_required"


@pytest.mark.parametrize("description, years, excluded", [
    ("Requires 5+ years of professional experience.", 5, True),
    ("3+ years of experience preferred.", None, False),
    ("0-2 years of experience with Python.", 0, False),
    ("Bachelor's degree and 2 years of relevant work experience.", 2, False),
    ("4 years of experience in lieu of a degree. 1+ years of software development experience.", 1, False),
])
def test_stated_experience_requirements(description, years, excluded):
    assert years_required(description) == years
    result = screen(facts("Software Engineer", description=description), RULES)
    assert (result.outcome == "excluded") is excluded
    assert result.signals["years_required"] == years


def test_explicit_experience_requirement_applies_even_to_new_grad_titles():
    result = screen(facts("New Grad Software Engineer", description="5+ years of experience required."), RULES)
    assert result.reasons == ("experience_required:5",) and result.signals["years_required"] == 5


@pytest.mark.parametrize("description, expected", [
    ("5+ years of software development experience and 2+ years of Python experience.", 5),
    ("Required:\n3 years of professional experience.\n1 year of distributed systems experience.", 3),
    ("2 years of Python experience and 5 years of software development experience.", 5),
    ("Bachelor's degree and 5 years of experience or a Master's degree and 2 years of experience.", 2),
    ("5 years of professional experience or 2 years of research experience.", 2),
    ("5 years of experience in C++ or Python and 2 years of cloud experience.", 5),
    ("5 years of experience or 2 years of research experience. 3 years of Python experience required.", 3),
    ("Bachelor's degree and 5 years of experience; or Master's degree and 2 years of experience.", 2),
    ("Bachelor's or Master's degree and 5 years of experience required.", 5),
    ("5 years of experience with a Bachelor's or Master's degree.", 5),
    ("At least 3 or 4 years of experience required.", 3),
    ("Bachelor's degree and 5 years of experience or a Master's degree.", 0),
    ("Bachelor's degree and 5 years of experience; or Master's degree.", 0),
])
def test_independent_experience_constraints_and_explicit_alternatives(description, expected):
    assert years_required(description) == expected
    result = screen(facts("Software Engineer", description=description), RULES)
    assert result.signals["years_required"] == expected
    assert (result.outcome == "excluded") is (expected >= 3)


def test_closed_at_source_excludes():
    assert screen(facts("Software Engineer", closed_at_source=True), RULES).reasons == ("closed_at_source",)


def test_rules_can_be_turned_off_and_change_the_version(tmp_path):
    relaxed = ScreeningRules(exclude_internships=False, max_years_required=None)
    assert screen(facts("Software Engineer Intern"), relaxed).outcome == "kept"
    assert relaxed.version != RULES.version
    path = tmp_path / "screening.json"
    path.write_text(json.dumps({"exclude_internships": False, "max_years_required": None,
                                "extra_non_cs_terms": ["support engineer"]}))
    loaded = load_rules(path)
    assert loaded == ScreeningRules(exclude_internships=False, max_years_required=None,
                                    extra_non_cs_terms=("support engineer",))
    assert screen(facts("Support Engineer"), loaded).reasons == ("not_cs_role",)


def test_missing_rules_file_means_defaults_and_bad_files_fail_loudly(tmp_path):
    assert load_rules(tmp_path / "missing.json") == ScreeningRules()
    assert load_rules(None) == ScreeningRules()
    bad = tmp_path / "bad.json"
    bad.write_text('{"exclude_interns": true}')
    with pytest.raises(ValueError, match="unknown keys"):
        load_rules(bad)
    bad.write_text('{"max_years_required": "three"}')
    with pytest.raises(ValueError, match="max_years_required"):
        load_rules(bad)


@pytest.mark.parametrize("description", [
    "U.S. citizenship is not required.",
    "You do not need an active Secret clearance.",
    "An active Secret clearance is not required for this position.",
    "No security clearance is required.",
    "The ability to obtain a security clearance is not required.",
    "You do not need to be a U.S. citizen.",
    "We welcome non-U.S. persons and international candidates.",
    "An active Secret clearance is preferred, but not required.",
    "U.S. citizens and non-citizens are welcome to apply.",
    "Experience building systems for U.S. persons is helpful.",
    "Do you have an active Secret clearance?",
    "Are you able to obtain a security clearance?",
    "This role does not require U.S. citizenship.",
    "Candidates need not meet ITAR requirements.",
])
def test_citizenship_mentions_without_a_requirement_do_not_exclude(description):
    result = screen(facts("Software Engineer", description=description), RULES)
    assert result.outcome == "kept"
    assert result.signals["sponsorship"] == "unknown"


@pytest.mark.parametrize("description, expected", [
    ("We are not able to sponsor visas.", "no"),
    ("We do not offer visa sponsorship.", "no"),
    ("Visa sponsorship is not unavailable.", "unknown"),
    ("We cannot guarantee that sponsorship will be available.", "unknown"),
    ("You do not need sponsorship to apply.", "unknown"),
    ("Candidates with or without sponsorship needs may apply.", "unknown"),
    ("Do you require visa sponsorship?", "unknown"),
])
def test_sponsorship_negation_and_questions_are_not_inverted(description, expected):
    result = screen(facts("Software Engineer", description=description), RULES)
    assert result.outcome == "kept"
    assert result.signals["sponsorship"] == expected


@pytest.mark.parametrize("description, expected", [
    ("5 years of experience is not required.", None),
    ("You do not need 5 years of experience.", None),
    ("No prior experience required. Our team has 20 years of experience.", None),
    ("Preferred qualifications:\n5+ years of experience in Python.", None),
    ("Preferred qualifications:\n5 years of experience.\nMinimum qualifications:\n2 years of experience.", 2),
    ("At least 3 years of experience is required; 5 years of experience preferred.", 3),
    ("No more than 2 years of experience.", None),
    ("Minimum 4 years of professional experience.", 4),
])
def test_negated_preferred_and_non_candidate_experience_does_not_exclude(description, expected):
    assert years_required(description) == expected


@pytest.mark.parametrize("title", [
    "Chief of Staff, New Grad Program", "Developer Advocate", "Member of Technical Staff",
    "Software Engineer - Staff Tools", "Software Engineer, Lead Generation",
    "Software Engineer, Manager Experience", "Staffing Software Engineer",
    "International Software Engineer",
])
def test_ambiguous_title_words_are_not_seniority_evidence(title):
    assert screen(facts(title), RULES).outcome == "kept"


def test_internship_source_name_requires_an_internship_token():
    result = screen(facts("Software Engineer", sources=("international-careers",)), RULES)
    assert result.outcome == "kept"


def test_explicit_citizenship_requirement_still_wins_over_separate_negated_requirement():
    result = screen(facts("Software Engineer", description=(
        "Security clearance is not required. Applicants must be a U.S. citizen.")), RULES)
    assert result.reasons == ("citizenship_required",)


@pytest.mark.parametrize("data", [
    [], {"exclude_internships": 1}, {"max_years_required": True},
    {"max_years_required": 0}, {"extra_cs_terms": "software"},
    {"extra_non_cs_terms": [""]},
])
def test_invalid_rules_are_rejected(tmp_path, data):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_rules(path)


def test_repository_and_title_case_does_not_change_explicit_internship():
    assert screen(facts("Software Engineer", employment_types=("Internship",)), RULES).reasons == ("internship",)


def test_explicit_non_cs_title_wins_over_generic_feed_category():
    assert screen(facts("Account Executive", categories=("Software",)), RULES).reasons == ("not_cs_role",)


def test_fpga_is_kept_even_when_feed_labels_it_hardware():
    assert screen(facts("FPGA Engineer", categories=("Hardware",)), RULES).outcome == "kept"


def test_custom_exclusion_terms_with_punctuation_match_whole_terms():
    rules = ScreeningRules(extra_non_cs_terms=("C++",))
    assert screen(facts("C++ Consultant"), rules).outcome == "excluded"
    assert screen(facts("C++20 Consultant"), rules).outcome == "kept"


@pytest.mark.parametrize("description", [
    "We are unable to offer visa sponsorship.",
    "We do not offer immigration sponsorship.",
    "We cannot offer employment visa sponsorship.",
    "We will not be able to provide sponsorship.",
    "We will never sponsor visas for this position.",
])
def test_negative_sponsorship_clauses_never_become_positive_signals(description):
    result = screen(facts("Software Engineer", description=description), RULES)
    assert result.outcome == "kept" and result.signals["sponsorship"] == "no"


@pytest.mark.parametrize("description", [
    "Candidates will not need sponsorship.",
    "Applicants can request sponsorship.",
    "Candidates will require visa sponsorship.",
])
def test_sponsorship_noun_is_not_the_employers_verb_sponsor(description):
    assert screen(facts("Software Engineer", description=description), RULES).signals["sponsorship"] == "unknown"
