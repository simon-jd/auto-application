"""Deterministic screening: only explicit evidence excludes; sponsorship is a signal."""

import json

import pytest

from auto_app.screen.rules import JobFacts, ScreeningRules, evidence, load_rules, screen, years_required

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
    ((), "Applicants must be a U.S. national."),  # "national" alone, so the keyword shortcut must keep it
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


# Real employer sentences from the live database (2026-11-06 audit). A US-person, citizenship or security-clearance
# requirement excludes; a hedged mention ("may", "where required", "certain positions") or a license route does not.
@pytest.mark.parametrize("description", [
    # SpaceX, Mach Industries, Rendezvous Robotics, Revel: an enumerated ITAR list defeated the old pattern.
    "ITAR REQUIREMENTS:\n\n- To conform to U.S. Government export regulations, applicant must be a (i) U.S. citizen "
    "or national, (ii) U.S. lawful, permanent resident (aka green card holder), (iii) Refugee under 8 U.S.C. § 1157, "
    "or (iv) Asylee under 8 U.S.C. § 1158, or be eligible to obtain the required authorizations from the U.S. "
    "Department of State. Learn more about the ITAR here.",
    # Saronic: the defined term is quoted.
    "If this role is based in the United States, it requires access to export-controlled information or items that "
    "require “U.S. Person” status.",
    # Muon Space.
    "To conform to U.S. Government export regulations applicable to that information, applicant must either be (A) a "
    "U.S. person, defined as a (i) U.S. citizen or national, (ii) U.S. lawful, permanent resident (green card "
    "holder), (iii) refugee under 8 U.S.C. § 1157, or (iv) asylee under 8 U.S.C. § 1158, (B) eligible to access the "
    "export controlled information without a required export authorization.",
    # Castelion: access without an export license.
    "This position requires access to export-controlled information, and as such, employment (or hiring of a "
    "contractor) is contingent upon the candidate’s ability to access all applicable export-controlled information "
    "without additional export licensing being required by the Bureau of Industry and Security.",
    # MatX.
    "This offer of employment is contingent upon the applicant's capacity to perform job functions in compliance "
    "with U.S. export control laws without obtaining a license from U.S. export control authorities.",
    # AeroVironment, K2 Space, Elveo.
    "In order to comply with the requirements of the ITAR and/or the EAR, applicants must qualify as a U.S. person "
    "under the ITAR and the EAR, or a person to be approved for an export license by the governing agency.",
    "The person hired for this role must therefore either be a “U.S. person” as defined by 22 C.F.R. § 120.62.",
    "To comply with U.S. Government export control regulations (ITAR), applicants must be one of the following: "
    "(i) a U.S. citizen or national, (ii) a lawful permanent resident (green card holder).",
    # Anduril, Shield AI: a clearance level between "U.S." and "clearance".
    "- Eligible to obtain and maintain an active U.S. Secret security clearance",
    "- Eligible to obtain and maintain an active U.S. Top Secret SCI security clearance.",
    "- Currently possesses and is able to maintain an active U.S. Secret security clearance",
    "- Must be able to obtain and hold a U.S. TS/SCI security clearance",
    "- Active U.S. Top Secret SCI clearance",
    "- Eligibility to obtain and maintain a U.S. clearance",
    "- US Persons status is required as this position needs to access export-controlled data",
    "- U.S. Person status (ITAR requirement)",
    # SpaceX, Booz Allen, Palantir, Microsoft, CACI, Nightwing.
    "- This position requires successfully obtaining and maintaining a Top Secret Security Clearance as a condition "
    "of employment.",
    "- Ability to obtain a Secret clearance",
    "TS/SCI clearance with polygraph is required.",
    "Basic Qualifications:\n- Python\n- Secret clearance",
    "- Active U.S. Security clearance, or eligibility and willingness to obtain a U.S. Security clearance.",
    "- The successful candidate must have an active U.S. Government Top Secret Security Clearance.",
    "There is no clearance requirement to begin employment. However, as a requirement of continued employment in "
    "this position you will be required to obtain a Top Secret clearance.",
    "Security Clearance\n\nTop Secret/SCI + Full Scope Poly required",
    "Minimum Clearance Required to Start: TS/SCI with Polygraph",
    # Restricted to U.S. persons or citizens.
    "- This role requires use of technical data subject to U.S. Government export restrictions and this posting is "
    "only for U.S. Persons (U.S. Citizens, lawful permanent residents and protected individuals).",
    "This position is open to U.S. Persons (U.S. citizens or lawful permanent residents) only.",
    "This role is restricted to U.S. persons (i.e., U.S. citizens, permanent residents, and other protected "
    "individuals) due to access to export-controlled technology.",
    "The position is subject to export control restrictions and requires the successful candidate to be a U.S. "
    "Person (U.S. citizen, U.S. permanent resident, asylee or refugee).",
    "Due to Federal requirements, only U.S. citizens, U.S. naturalized citizens or U.S. Permanent Residents, holding "
    "a green card, will be considered.",
    "- US citizens or Green Card ONLY",
    "***US Citizen OR Green Card Holder required***",
    "Permanent work authorization (U.S. Citizen or Green Card holder) is required.",
    "- U.S. Citizen or Lawful Permanent Resident (Green Card)",
    "- US Persons Only",
])
def test_us_person_citizenship_and_clearance_requirements_exclude(description):
    result = screen(facts("Software Engineer", description=description), RULES)
    assert result.reasons == ("citizenship_required",), description
    assert result.signals["sponsorship"] == "citizenship_required"


@pytest.mark.parametrize("description", [
    # Cloudflare, Infleqtion, Databricks, Analog Devices, OpenAI, GE: export control with a license route or a hedge.
    "This position may require access to information protected under U.S. export control laws, including the U.S. "
    "Export Administration Regulations.",
    "Please note that any offer of employment may be conditioned on your authorization to receive software or "
    "technology controlled under these U.S. export laws without sponsorship for an export license.",
    "If access to export-controlled technology or source code is required for performance of job duties, it is "
    "within Employer's discretion whether to apply for a U.S. government license for such positions.",
    "As such, applicants for this position – except US Citizens, US Permanent Residents, and protected individuals "
    "as defined by 8 U.S.C. 1324b(a)(3) – may have to go through an export licensing review process.",
    "To comply with U.S. export control laws and regulations, candidates for this role may need to meet certain "
    "legal status requirements as provided in those laws and regulations.",
    "This role requires access to U.S. export-controlled information. If applicable, final offers will be "
    "contingent on ability to obtain authorization for access to U.S. export-controlled information from the U.S. "
    "Government.",
    "An offer of employment will be contingent upon the applicant's capacity to perform in compliance with U.S. "
    "export control laws.",
    # Microsoft, Everfox, ZT Systems, Castelion, Samsung, Trellisware: hedged.
    "Specifically, this position supports United States federal government agency customers and is subject to "
    "certain citizenship-based restrictions where required or permitted by applicable law.",
    'Applicants for these positions may need to be "U.S. Persons," as defined in these regulations.',
    "Please be aware that certain positions may require the applicant to either 1) be a (i) U.S. citizen or "
    "national, (ii) U.S. lawful, permanent resident (aka green card holder).",
    "If the position is so designated, your employment in the role may be contingent upon your ability to obtain "
    "and maintain the required U.S. Government security clearance or other government authorization.",
    "Employment may be contingent on either the applicant verifying that the applicant falls under the “U.S. "
    "Person” definition or the employer timely obtaining any necessary export license under federal law.",
    'Technical job openings typically require the ability to access export-controlled information and are '
    'generally limited to "U.S. persons" as defined under the EAR and ITAR.',
    # Definitions, pay and benefits text, unrelated clearances, and statuses that include non-U.S. persons.
    "As defined by U.S. law, individuals who are any one of the following are considered to be a “U.S. Person”: "
    "(1) U.S. citizens, (2) legal permanent residents (a.k.a. green card holders).",
    "Applicants selected will be subject to a security investigation and may need to meet eligibility "
    "requirements for access to classified information.",
    "Those with an active clearance will receive a 10% differential, up to an additional $20,000 annually.",
    "Compensation decisions depend on a wide range of factors, including skill sets, experience and training, "
    "security clearances, licensure and certifications.",
    "- Keep supply chains moving by managing the essential documents and data needed for smooth customs clearance.",
    "- Ability to obtain a S//SAR level security clearance desired.",
    "- Active U.S. Security clearance, or eligibility and willingness to obtain a U.S. Security clearance is "
    "beneficial, but not necessary.",
    "Minimum Clearance Required to Start: None",
    "Security Clearance\n\nNone required",
    "Acme holds a Top Secret facility security clearance.",
    "Work Authorization: OPT | STEM OPT | CPT | H4-EAD | Green Card | US Citizen",
    "Only H1B, US Citizens and Green Card holders will be considered.",
    "US Citizens, Green Card Holders, and those authorized to work in the US for any employer will be considered.",
    "We consider all applicants without regard to race, color, national origin, citizenship status or disability.",
])
def test_hedged_or_licensable_export_mentions_do_not_exclude(description):
    result = screen(facts("Software Engineer", description=description), RULES)
    assert result.outcome == "kept", description
    assert result.signals["sponsorship"] == "unknown"


@pytest.mark.parametrize("title", [
    "Mission Software Engineer, Vehicle Software, Active Clearance",
    "Junior Embedded RUST Software Engineer (cleared)",
    "Software Developer - Multiple Levels - Cleared",
    "Junior Software Engineer with Security Clearance",
    "Full-Stack Software Developer, TS/SCI with Polygraph",
    "Software Engineer, C++ - Top Secret Clearance",
    "Platform Software Engineer II - US Citizenship Required",
    "Future IT Leaders - Recent Grad/Full Time (US Person Required)",
    "UI React Engineer - Polygraph Required",
    "Junior Data Engineer / Active Secret",
])
def test_clearance_or_citizenship_in_the_title_excludes(title):
    result = screen(facts(title), RULES)
    assert result.reasons == ("citizenship_required",), title


@pytest.mark.parametrize("title", ["Customs Clearance Software Engineer", "Software Engineer, FDA-cleared Devices",
                                   "Security Software Engineer", "Software Engineer, Secrets Management"])
def test_unrelated_title_words_are_not_clearance_evidence(title):
    assert screen(facts(title), RULES).signals["sponsorship"] == "unknown", title


@pytest.mark.parametrize("description, expected", [
    # Seres Auto (job 11504): a tilde range states its lower bound.
    ("- 1~3 years of professional embedded firmware development experience in industry.", 1),
    ("1 ~ 3 years of software development experience required.", 1),
    # K2 Space (job 1248): a degree in a named field, or experience instead.
    ("- Bachelor's degree in computer science, computer engineering, electrical engineering, math, or a STEM "
     "discipline or 3+ years of professional experience in software engineering", 0),
    # Cisco (job 13042): a list of alternative routes.
    ("Minimum Qualifications\n\n-\nMeet one of the following (completed within the past 3 years or expected to be "
     "completed within the next 12 months):\n-A Master's degree with 0 years of work experience;\n-Bachelor's degree "
     "with 2 years of work experience;\n-An educational or workforce development program (e.g., Associate’s, "
     "Apprenticeship, Boot camp, or Certification) with 3 years of work experience;\n-High school diploma (or "
     "equivalent) with 4 years of work experience. Qualifying education and work experience should be in Computer "
     "Science or a related field.\n\n-\nStrong proficiency in C or C++ systems programming", 0),
])
def test_ranges_and_degree_alternatives_are_not_hard_years(description, expected):
    assert years_required(description) == expected


@pytest.mark.parametrize("description, expected", [
    # Real >= 3-year requirements stay hard.
    ("Bachelor's degree in Computer Science or a related field and 3+ years of professional experience.", 3),
    ("Bachelor's degree in Computer Science or Master's degree in Computer Science and 3 years of experience.", 3),
    ("Bachelor's and 5 years related experience or Master's degree and 3 years related experience in computer "
     "science.", 3),
    ("3~5 years of embedded experience required.", 3),
    ("Meet one of the following:\n- Bachelor's degree and 5 years of experience\n- Master's degree and 3 years of "
     "experience", 3),
    ("Experience with one of the following: Python, Java or Go.\n- 4+ years of backend experience.", 4),
    ("Meet one of the following:\n- Bachelor's degree with 2 years of experience\n- Master's degree with 0 years of "
     "experience\n\nRequirements:\n- 5+ years of Python experience", 5),
])
def test_real_multi_year_requirements_stay_hard(description, expected):
    assert years_required(description) == expected


@pytest.mark.parametrize("description", [
    # Pay statements, airport badges, HR duties and headings mention clearances without requiring one.
    "Compensation decisions depend on a wide range of factors, including but not limited to skill sets, experience "
    "and training, security clearances, licensure and certifications, and other business and organizational needs.",
    "Final compensation is determined based on the role's requirements, relevant skills, experience, education, "
    "certifications, work location, applicable security clearance requirements, market conditions, and internal equity.",
    "Transportation Security Administration (TSA) Security Clearance: Candidates for employment with the San "
    "Francisco Airport Commission are required to provide a complete employment history for the past ten years.",
    "Maintain responsibility for updates to various databases, security clearance processing, and review "
    "documentation for accuracy and completeness.",
    "Work Authorization/security Clearance Requirements",
    # Statements that also admit non-citizens, or that only say who U.S. persons include.
    "Bellevue College employs only U.S. citizens and lawfully authorized non-U.S. citizens.",
    "Viable candidates for this position must be eligible for an export license or be a U.S. Person (which includes "
    "but is not limited to U.S. citizens or nationals, lawful permanent residents, refugees, and asylees).",
    "This position may require the ability to obtain and maintain a U.S. government security clearance.",
    # Preferred sections (NVIDIA, Handshake, Anthropic).
    "Ways to stand out from the crowd:\n\n-\nDomain expertise in data and graph analytics.\n\n-\nExperience working "
    "within the Federal Government and the ability to hold a US security clearance",
    "Nice to Have\n\n - Active or prior security clearance (Secret, Top Secret, or SCI)\n - Experience in threat "
    "assessment",
    "Preferred qualifications\n\n- Have taken a service through FedRAMP High\n\n- Hold or are eligible for a US security "
    "clearance",
    "Preferred qualifications\n-\nExperience with AI/ML evaluation frameworks\n-\nActive secret security clearance or "
    "higher, or eligibility to obtain one",
])
def test_clearance_mentions_that_are_not_the_candidates_requirement_do_not_exclude(description):
    result = screen(facts("Software Engineer", description=description), RULES)
    assert result.outcome == "kept", description


@pytest.mark.parametrize("description", [
    "Nice to Have\n- Rust\n\nITAR Requirements\n\nApplicants must be U.S. persons.",
    "Preferred Qualifications:\n- Rust\nAdditional Requirements:\n- Must hold an active Secret clearance.",
    # A paragraph after a preferred list (Advanced Space, CACI), and unmarked items that merely say "a plus".
    "Bonus\n- Rust\n\nThis position is open to U.S. Persons (U.S. citizens or lawful permanent residents) only.",
    "These Qualifications Would Be Nice to Have:\n- Python\n\nThere is no clearance requirement to begin "
    "employment. However, as a requirement of continued employment in this position you will be required to obtain "
    "a Top Secret clearance.",
    "Robotics experience is a plus\nIncumbent MUST be either a U.S. citizen, lawful permanent resident or have "
    "refugee or asylee status as this role is subject to ITAR.",
    # An explicit requirement listed under a preferred heading (Archer, Anduril).
    "Preferred Experience And Qualifications\n- Comfort with debugging systems.\n-\nSecret Clearance required.",
    "PREFERRED QUALIFICATIONS\n- U.S. Person status is required as this position needs to access export controlled "
    "data. Eligibility to obtain/maintain a US Top Secret clearance is also desirable.",
    # A pay word after the clearance requirement on one run-on line (Altamira).
    "Must Have: TS/SCI clearance Bachelor's degree in CS 2+ years of experience developing tools, salary $120k",
])
def test_a_requirement_after_a_preferred_section_still_excludes(description):
    assert screen(facts("Software Engineer", description=description), RULES).reasons == ("citizenship_required",)


NOISE = ScreeningRules(exclude_unknown_without_software_evidence=True)


def test_software_evidence_rule_is_off_by_default_and_loads_from_the_rules_file(tmp_path):
    assert ScreeningRules().exclude_unknown_without_software_evidence is False
    path = tmp_path / "screening.json"
    path.write_text(json.dumps({"exclude_unknown_without_software_evidence": True}))
    assert load_rules(path) == NOISE and NOISE.version != RULES.version


def test_unknown_role_without_software_evidence_is_excluded_when_enabled():
    job = facts("Engineer", description="Coordinate vendor shipments and maintain warehouse safety records.")
    assert screen(job, NOISE).reasons == ("no_software_evidence",)
    assert screen(job, RULES).outcome == "kept"  # the flag off leaves the job unchanged


@pytest.mark.parametrize("description", [
    "Automate reports with Python.", "Write C++ for the trading engine.", "Build services in C#.",
    "Maintain the CI/CD pipelines.", "A degree in Computer Science.", "Work with REST APIs and SQL databases.",
])
def test_unknown_role_with_software_evidence_is_kept(description):
    assert screen(facts("Engineer", description=description), NOISE).outcome == "kept", description


def test_software_evidence_rule_needs_a_description_and_an_unknown_role():
    assert screen(facts("Engineer"), NOISE).outcome == "kept"
    assert screen(facts("Engineer", description="   "), NOISE).outcome == "kept"
    assert screen(facts("Software Engineer", description="Join a friendly team."), NOISE).outcome == "kept"


@pytest.mark.parametrize("title", [
    "Software Engineer, Sr. Staff - Power and Limits Management",
    "Systems Engineer - Chipset Power, Senior Staff",
    "Software Engineer - Sr Staff",
    "Firmware Engineer, Principal Lead",
])
def test_a_title_part_made_only_of_level_words_is_senior(title):
    assert "senior_title" in screen(facts(title), RULES).reasons, title


@pytest.mark.parametrize("title", [
    "Member of Technical Staff - Inference", "Software Engineer - Staffing Platform",
    "Software Engineer, Lead Generation", "Software Engineer - Staff Tools",
])
def test_team_suffixes_with_level_words_stay_kept(title):
    assert screen(facts(title), RULES).outcome == "kept", title


def test_evidence_read_once_judges_a_job_under_any_rules():
    """A rule-change preview reads each description once (evidence) and judges it under both rule sets."""
    job = facts("Senior Platform Analyst", locations=("Toronto, ON",), description=(
        "Security Clearance\n\nTop Secret/SCI required\n"
        "Bachelor's degree and 5 years of experience; or Master's degree.\n"
        "Requirements:\n- 4 years of experience with Python\nWe do not offer visa sponsorship."))
    read = evidence(job)
    for rules in (RULES, ScreeningRules(exclude_senior=False, us_only=False, max_years_required=None,
                                        exclude_unknown_without_software_evidence=True,
                                        extra_non_cs_terms=("analyst",))):
        assert screen(job, rules, read) == screen(job, rules)
    assert (read.sponsorship, read.years_required, read.location) == ("citizenship_required", 4, "non_us")
