"""Public Markdown feeds retain evidence without inventing employer status."""

from importlib import import_module

import pytest


SPEEDY_HEADER = "| Company | Position | Location | Salary | Posting | Age |\n|---|---|---|---|---|---|\n"
SPEEDY_NO_SALARY = "| Company | Position | Location | Posting | Age |\n|---|---|---|---|---|\n"
JOBRIGHT_HEADER = "| Company | Job Title | Location | Work Model | Date Posted |\n| ----- | --------- | --------- | ---- | ------- |\n"


def parse(provider, text):
    parser = getattr(import_module("auto_app.ingest.repository_markdown"), f"parse_{provider}")
    return parser(text.encode() if isinstance(text, str) else text)


def speedy_row(company="Example", title="Backend Intern", location="Seattle, WA", salary="$52/hr",
               url="https://careers.example.com/jobs/123", age="1d"):
    return (f'| <a href="https://example.com"><strong>{company}</strong></a> | {title} | '
            f'{location} | {salary} | <a href="{url}"><img src="https://images.example/apply.png" '
            f'alt="Apply"/></a> | {age} |\n')


def jobright_row(company="**[Example](https://example.com)**", title="Backend Engineer", location="Boston, MA",
                 url="https://jobright.ai/jobs/info/abc123?utm_source=git", date="Oct 10"):
    return f"| {company} | **[{title}]({url})** | {location} | Hybrid | {date} |\n"


def test_speedy_html_links_locations_and_salary_survive_as_source_evidence():
    raw = speedy_row(company="Research &amp; Systems", title="C++ &amp; Backend Intern",
                     location="Seattle, WA<br/>Toronto, ON, Canada",
                     url="https://careers.example.com/jobs/123?utm_source=feed&req=123")
    result = parse("speedyapply", "# Internships\n\n" + SPEEDY_HEADER + raw)
    assert result.issues == []
    [record] = result.records
    assert record.url == "https://careers.example.com/jobs/123?req=123"
    assert record.company == "Research & Systems"
    assert record.title == "C++ & Backend Intern"
    assert record.locations == ["Seattle, WA", "Toronto, ON, Canada"]
    assert record.posted_at is None and record.active is None
    assert record.employment_type == "unknown" and record.description == ""
    assert record.metadata["upstream"]["salaries"] == ["$52/hr"]
    [evidence] = record.metadata["source_observation"]["rows"]
    assert evidence["raw"] == raw.strip()
    assert evidence["cells"]["age"] == "1d"
    assert "Toronto, ON, Canada" in evidence["cells"]["location"]


def test_speedy_repeated_tables_and_optional_salary_merge_repeated_job_locations():
    document = SPEEDY_HEADER + speedy_row()
    document += "\n## Other roles\n" + SPEEDY_NO_SALARY
    document += "| **[Example](https://example.com)** | Backend Intern | Ottawa, ON, Canada | [Apply](https://careers.example.com/jobs/123) | 1d |\n"
    result = parse("speedyapply", document)
    assert result.issues == []
    [record] = result.records
    assert record.locations == ["Ottawa, ON, Canada", "Seattle, WA"]
    assert record.metadata["upstream"]["salaries"] == ["$52/hr"]
    assert len(record.metadata["source_observation"]["rows"]) == 2


def test_identical_repeated_rows_are_not_partial_failures():
    raw = speedy_row()
    result = parse("speedyapply", SPEEDY_HEADER + raw + raw)
    assert len(result.records) == 1
    assert result.issues == []


def test_jobright_carry_forward_company_keeps_canadian_location_and_source_job_id():
    document = JOBRIGHT_HEADER + jobright_row()
    document += jobright_row(company="↳", location="Ottawa, ON, Canada", url="https://jobright.ai/jobs/info/def456")
    result = parse("jobright", document)
    assert result.issues == []
    first, second = result.records
    assert first.external_id == "jobright:abc123"
    assert first.url == "https://jobright.ai/jobs/info/abc123"
    assert first.company == second.company == "Example"
    assert second.locations == ["Ottawa, ON, Canada"]
    assert second.metadata["upstream"]["company_urls"] == ["https://example.com/"]
    assert second.metadata["upstream"]["work_models"] == ["Hybrid"]
    assert second.metadata["source_observation"]["rows"][0]["cells"]["company"] == "↳"
    assert first.active is None and first.posted_at is None


def test_markdown_link_labels_with_literal_pipes_and_parentheses_are_not_extra_columns():
    result = parse("jobright", JOBRIGHT_HEADER + jobright_row(
        company="**[Example (US) | Exemple](https://example.com/path_(us))**",
        title=r"Backend \| Platform (Intern)",
    ))
    assert result.issues == []
    [record] = result.records
    assert record.company == "Example (US) | Exemple"
    assert record.title == "Backend | Platform (Intern)"
    assert record.metadata["upstream"]["company_urls"] == ["https://example.com/path_(us)"]


@pytest.mark.parametrize("provider,header,row", [
    ("speedyapply", SPEEDY_HEADER, speedy_row()),
    ("jobright", JOBRIGHT_HEADER, jobright_row()),
])
def test_age_date_and_row_position_changes_do_not_change_identity_or_stable_metadata(provider, header, row):
    before = parse(provider, header + row).records[0]
    later = parse(provider, "# Updated\n\n" + header + row.replace("1d", "2d").replace("Oct 10", "Jan 01")).records[0]
    assert before.external_id == later.external_id
    assert before.posted_at is later.posted_at is None
    assert before.metadata["upstream"] == later.metadata["upstream"]
    assert before.metadata["source_observation"] != later.metadata["source_observation"]


def test_title_and_location_edits_keep_identity_but_stable_content_changes():
    before = parse("speedyapply", SPEEDY_HEADER + speedy_row()).records[0]
    after = parse("speedyapply", SPEEDY_HEADER + speedy_row(title="Platform Intern", location="Austin, TX")).records[0]
    assert before.external_id == after.external_id
    assert before.title != after.title and before.locations != after.locations


@pytest.mark.parametrize("provider,header,bad_row,good_row", [
    ("speedyapply", SPEEDY_HEADER, speedy_row(url="javascript:alert(1)"), speedy_row(url="https://careers.example.com/jobs/valid")),
    ("jobright", JOBRIGHT_HEADER, jobright_row(url="/jobs/info/123"), jobright_row(url="https://jobright.ai/jobs/info/valid")),
    ("speedyapply", SPEEDY_HEADER, "| too | few | columns |\n", speedy_row()),
    ("jobright", JOBRIGHT_HEADER, jobright_row(title=""), jobright_row()),
])
def test_bad_rows_report_issues_without_dropping_good_siblings(provider, header, bad_row, good_row):
    result = parse(provider, header + bad_row + good_row)
    assert len(result.records) == 1
    assert len(result.issues) == 1
    assert result.issues[0]["index"] == 0
    assert result.issues[0]["line"] == 3


def test_carry_forward_does_not_cross_table_boundaries():
    document = JOBRIGHT_HEADER + jobright_row()
    document += "\n## Another table\n" + JOBRIGHT_HEADER + jobright_row(company="↳", url="https://jobright.ai/jobs/info/second")
    result = parse("jobright", document)
    assert len(result.records) == 1
    assert len(result.issues) == 1 and "company" in result.issues[0]["reason"]


def test_same_job_link_with_company_and_title_variants_keeps_all_observed_labels_stably():
    # The live IDeaS/SAS duplicate uses the same concrete application URL.
    first = speedy_row(company="IDeaS", title="Backend Intern")
    second = speedy_row(company="SAS", title="Software Intern")
    result = parse("speedyapply", SPEEDY_HEADER + first + second)
    reverse = parse("speedyapply", SPEEDY_HEADER + second + first)
    assert result.issues == reverse.issues == []
    [record], [reordered] = result.records, reverse.records
    assert (record.title, record.company) == (reordered.title, reordered.company) == ("Backend Intern", "IDeaS")
    assert record.metadata["upstream"] == reordered.metadata["upstream"]
    assert record.metadata["upstream"]["company_variants"] == ["IDeaS", "SAS"]
    assert record.metadata["upstream"]["title_variants"] == ["Backend Intern", "Software Intern"]
    assert len(record.metadata["source_observation"]["rows"]) == 2


def test_duplicate_locations_are_stable_when_source_row_order_changes():
    first, second = speedy_row(location="Boston, MA"), speedy_row(location="Toronto, ON, Canada")
    before = parse("speedyapply", SPEEDY_HEADER + first + second).records[0]
    after = parse("speedyapply", SPEEDY_HEADER + second + first).records[0]
    assert before.external_id == after.external_id
    assert before.locations == after.locations == ["Boston, MA", "Toronto, ON, Canada"]
    assert before.metadata["upstream"] == after.metadata["upstream"]


def test_html_location_list_preserves_country_without_treating_summary_as_a_location():
    result = parse("speedyapply", SPEEDY_HEADER + speedy_row(
        location="<details><summary>2 locations</summary><ul><li>Boston, MA</li><li>Toronto, Canada</li></ul></details>",
    ))
    assert result.records[0].locations == ["Boston, MA", "Toronto, Canada"]


def test_hidden_extra_locations_are_preserved_without_guessing_countries():
    result = parse("speedyapply", SPEEDY_HEADER + speedy_row(location="Sunnyvale, CA +6"))
    assert result.records[0].locations == ["Sunnyvale, CA +6"]


def test_different_tracking_links_share_a_stable_speedy_job_identity():
    first = parse("speedyapply", SPEEDY_HEADER + speedy_row(url="https://careers.example.com/jobs/123?utm_source=a")).records[0]
    second = parse("speedyapply", SPEEDY_HEADER + speedy_row(url="https://careers.example.com/jobs/123?utm_source=b#apply")).records[0]
    assert first.external_id == second.external_id


def test_markdown_apply_badge_uses_outer_job_link_and_rejects_image_only_link():
    badge = "[![Apply](https://images.example/apply.png)](https://careers.example/jobs/1)"
    document = SPEEDY_NO_SALARY + f"| Example | Backend Intern | Boston, MA | {badge} | 1d |\n"
    document += "| Example | Missing link | Boston, MA | ![Apply](https://images.example/apply.png) | 1d |\n"
    result = parse("speedyapply", document)
    assert [record.url for record in result.records] == ["https://careers.example/jobs/1"]
    assert len(result.issues) == 1


def test_malformed_row_without_leading_pipe_is_partial_and_keeps_following_rows():
    document = JOBRIGHT_HEADER + jobright_row()
    document += jobright_row(company="Broken", url="https://jobright.ai/jobs/info/bad").lstrip("| ")
    document += jobright_row(url="https://jobright.ai/jobs/info/last")
    result = parse("jobright", document)
    assert [record.external_id for record in result.records] == ["jobright:abc123", "jobright:last"]
    assert len(result.issues) == 1


@pytest.mark.parametrize("provider,header", [("speedyapply", SPEEDY_HEADER), ("jobright", JOBRIGHT_HEADER)])
def test_supported_empty_table_is_a_complete_empty_snapshot(provider, header):
    result = parse(provider, header)
    assert result.records == [] and result.issues == []


@pytest.mark.parametrize("document", [
    "# No current job table", "<html>Gateway Error</html>", "[]", b"\xff",
    "| Company | Position | Location | Posting | Age |\nmissing delimiter\n",
    "| Company | Position | Location | Different link | Age |\n|---|---|---|---|---|\n",
    "```markdown\n" + SPEEDY_HEADER + speedy_row() + "```\n",
])
def test_unrecognized_or_broken_document_never_looks_like_successful_empty_snapshot(document):
    with pytest.raises(ValueError):
        parse("speedyapply", document)


def test_unknown_listing_header_after_valid_table_is_structural_failure():
    document = SPEEDY_HEADER + speedy_row()
    document += "\n| Company | Position | Place | Apply now |\n|---|---|---|---|\n"
    with pytest.raises(ValueError):
        parse("speedyapply", document)
