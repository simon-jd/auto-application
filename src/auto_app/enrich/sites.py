"""Employer sites read only for enrichment (no collector): Microsoft and other Eightfold tenants,
TikTok/ByteDance, Jobright, SmartRecruiters, Workable, Amazon and Oracle Recruiting Cloud (docs/linux-worker.md).

Each reader takes the response body and the requested Target and returns the job as a CommonPosting,
None when the site answers but no longer lists the job (cached as gone), or raises ValueError when the
response is not the requested job. Readers never touch the network.
"""

import html as htmllib
import json
import re
from datetime import datetime, timezone

from auto_app.core.models import CommonPosting, Salary
from auto_app.enrich.targets import SITE_LINK, Target
from auto_app.sources.api import parse_iso
from auto_app.sources.common.text import (as_utc, clean_lines, employment_of, html_to_text, parse_salary,
                                          salary_from_description, workplace_of)

PAGES = {"tiktok", "jobright"}  # HTML pages; the other sites answer JSON
TIKTOK_COMPANY = {"lifeattiktok.com": "TikTok", "joinbytedance.com": "ByteDance"}
TIKTOK_EMPLOYMENT = {"regular": "full_time", "intern": "internship", "internship": "internship"}
SR_EMPLOYMENT = {"permanent": "full_time", "full_time": "full_time", "part_time": "part_time", "contract": "contract",
                 "temporary": "temporary", "intern": "internship", "internship": "internship"}
WORKABLE_EMPLOYMENT = {"full": "full_time", "part": "part_time", "contract": "contract", "temporary": "temporary",
                       "internship": "internship"}
WORKABLE_WORKPLACE = {"on_site": "onsite", "hybrid": "hybrid", "remote": "remote"}
PERIOD = {"year": "year", "yearly": "year", "month": "month", "monthly": "month", "week": "week", "hour": "hour",
          "hourly": "hour"}


def _title(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("record must have a title")
    return value.strip()


def _strings(values) -> list[str]:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()] if isinstance(values, list) else []


def _country(codes) -> str | None:
    """One ISO alpha-2 country for every location, else None."""
    found = {c.strip().upper() for c in codes if isinstance(c, str) and re.fullmatch(r"\s*[A-Za-z]{2}\s*", c)}
    return found.pop() if len(found) == 1 else None


def _paragraph(value) -> str:
    return f"<p>{htmllib.escape(value.strip())}</p>" if isinstance(value, str) and value.strip() else ""


def _bullets(values) -> str:
    items = _strings(values)
    return "<ul>" + "".join(f"<li>{htmllib.escape(v)}</li>" for v in items) + "</ul>" if items else ""


def _sections(parts) -> tuple[str, str | None]:
    """[(heading or None, html)] → (text, html); empty sections are left out."""
    texts, htmls = [], []
    for heading, body in parts:
        if body is not None and not isinstance(body, str):
            raise ValueError("description must be text")
        if not (text := html_to_text(body)):
            continue
        heading = heading.strip() if isinstance(heading, str) and heading.strip() else None
        texts.append(f"{heading}\n{text}" if heading else text)
        htmls.append((f"<h3>{htmllib.escape(heading)}</h3>" if heading else "") + body)
    return "\n\n".join(texts), "".join(htmls) or None


def _posting(target: Target, **fields) -> CommonPosting:
    link = SITE_LINK[target.platform](target.board, target.job_id)
    fields.setdefault("url", link)
    fields.setdefault("apply_url", link)
    return CommonPosting(source="enrichment", source_job_id=target.job_id, apply_kind="external", **fields)


def microsoft(data, target: Target) -> CommonPosting:
    """apply.careers.microsoft.com/api/pcsx/position_details → {"data": position}; a closed position is a 404."""
    return _pcsx(data, target, "Microsoft")


def eightfold(data, target: Target) -> CommonPosting:
    """<tenant>.eightfold.ai/api/pcsx/position_details: Microsoft's position API, for any Eightfold tenant (the
    eightfold-board feeds). The record never names the employer; the feed's company stays on the posting."""
    return _pcsx(data, target, None)


def _pcsx(data, target: Target, company: str | None) -> CommonPosting:
    record = data.get("data") if isinstance(data, dict) else None
    if not isinstance(record, dict) or str(record.get("id")) != target.job_id:
        raise ValueError("record does not match requested job")
    text, html = _sections([(None, record.get("jobDescription"))])
    posted = record.get("postedTs")
    return _posting(
        target, title=_title(record.get("name")), company=company,
        locations=_strings(record.get("locations")) or _strings([record.get("location")]),
        workplace_type=workplace_of(record.get("workLocationOption")),
        employment_type=employment_of((_strings(record.get("efcustomTextEmploymentType")) or [""])[0]),
        salary=salary_from_description(text),
        posted_at=datetime.fromtimestamp(posted, tz=timezone.utc) if type(posted) is int else None,
        posted_at_precision="exact" if type(posted) is int else None,
        req_id=str(record.get("displayJobId") or "") or None, description_text=text, description_html=html,
        extras={"department": record.get("department"),
                "country": _country(loc.rsplit(",", 1)[-1] for loc in _strings(record.get("standardizedLocations")))})


def _meta_title(page: str) -> str:
    m = (re.search(r'<meta property="og:title" content="([^"]*)"', page)
         or re.search(r"<title[^>]*>([^<]*)</title>", page))
    return htmllib.unescape(m[1]).strip() if m else ""


def tiktok(page, target: Target) -> CommonPosting | None:
    """lifeattiktok.com / joinbytedance.com /search/<id>: the job is rendered server-side as
    title, "Label:" / value pairs, the apply link, "Share this listing:", the description, the apply link again."""
    if not isinstance(page, str):
        raise ValueError("page must be text")
    title, marker = _meta_title(page), f"/resume/{target.job_id}/apply"
    if marker not in page:
        if not title and "self.__next_f" in page:
            return None  # the career site rendered its page without this job
        raise ValueError("page does not show the requested job")
    visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", page, flags=re.S | re.I)
    lines = html_to_text(visible).splitlines()
    try:
        share = lines.index("Share this listing:")
        end = lines.index("Apply to this job", share)
    except ValueError:
        raise ValueError("job page layout not recognized") from None
    start = lines.index(title) + 1 if title in lines[:share] else max(0, share - 12)
    head = [line for line in lines[start:share] if line]
    fields = {label[:-1].strip(): value for label, value in zip(head, head[1:])
              if label.endswith(":") and not value.endswith(":")}
    text = clean_lines("\n".join(lines[share + 1:end]))
    kind = fields.get("Employment Type", "")
    return _posting(
        target, title=_title(title), company=TIKTOK_COMPANY.get(target.board),
        locations=_strings([fields.get("Location")]),
        employment_type=TIKTOK_EMPLOYMENT.get(kind.lower()) or employment_of(kind),
        salary=salary_from_description(text), req_id=fields.get("Job Code") or None, description_text=text,
        extras={"team": fields.get("Team")})


def jobright(page, target: Target) -> CommonPosting | None:
    """jobright.ai/jobs/info/<id>: the logged-out page's __NEXT_DATA__. Its description is Jobright's own
    structured summary of the employer's JD (summary, responsibilities, qualifications, benefits)."""
    if not isinstance(page, str):
        raise ValueError("page must be text")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        raise ValueError("page has no job data")
    source = json.loads(m[1])["props"]["pageProps"]["dataSource"]
    job, company = source.get("jobResult"), source.get("companyResult") or {}
    if not isinstance(job, dict) or str(job.get("jobId") or "").lower() != target.job_id:
        raise ValueError("record does not match requested job")
    if job.get("isDeleted") is True:
        return None
    qualifications = job.get("qualifications") or {}
    text, html = _sections([
        (None, _paragraph(job.get("jobSummary"))),
        ("Responsibilities", _bullets(job.get("coreResponsibilities"))),
        ("Qualifications", _bullets(qualifications.get("mustHave"))),
        ("Preferred Qualifications", _bullets(qualifications.get("preferredHave"))),
        ("Benefits", _bullets(job.get("benefitsSummaries")))])
    workplace = workplace_of(job.get("workModel"))
    posted = as_utc(parse_iso(job.get("publishTime") if isinstance(job.get("publishTime"), str) else None))
    return _posting(
        target, title=_title(job.get("jobTitle")),
        company=company.get("companyName") if isinstance(company.get("companyName"), str) else None,
        locations=_strings(job.get("jobLocations")) or _strings([job.get("jobLocation")]),
        workplace_type="remote" if workplace == "unknown" and job.get("isRemote") is True else workplace,
        employment_type=employment_of(job.get("employmentType")),
        salary=parse_salary(job.get("salaryDesc"), "structured") if isinstance(job.get("salaryDesc"), str) else None,
        posted_at=posted, posted_at_precision="exact" if posted else None, description_text=text,
        description_html=html, extras={"country": _country([job.get("countryCode")])})


def smartrecruiters(data, target: Target) -> CommonPosting | None:
    """api.smartrecruiters.com/v1/companies/<company>/postings/<id or uuid>."""
    if not isinstance(data, dict) or target.job_id not in (str(data.get("id") or ""), str(data.get("uuid") or "").lower()):
        raise ValueError("record does not match requested job")
    if data.get("active") is False:
        return None
    sections = (data.get("jobAd") or {}).get("sections") or {}
    text, html = _sections([(s.get("title"), s.get("text")) for key in
                            ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
                            if isinstance(s := sections.get(key), dict)])
    loc = data.get("location") or {}
    place = loc.get("fullLocation") or ", ".join(_strings([loc.get("city"), loc.get("region"), loc.get("country")]))
    if loc.get("remote") is True:
        workplace = "remote"
    elif loc.get("hybrid") is True:
        workplace = "hybrid"
    else:
        workplace = "onsite" if loc.get("remote") is False and loc.get("hybrid") is False else "unknown"
    kind = data.get("typeOfEmployment") or {}
    posted = parse_iso(data.get("releasedDate") if isinstance(data.get("releasedDate"), str) else None)
    return _posting(
        target, title=_title(data.get("name")), company=(data.get("company") or {}).get("name"),
        locations=_strings([place]), workplace_type=workplace,
        employment_type=SR_EMPLOYMENT.get(kind.get("id")) or employment_of(kind.get("label")),
        salary=salary_from_description(text), posted_at=posted, posted_at_precision="exact" if posted else None,
        req_id=data.get("refNumber") or None, description_text=text, description_html=html,
        extras={"country": _country([loc.get("country")])})


def workable(data, target: Target) -> CommonPosting:
    """apply.workable.com/api/v2/accounts/<account>/jobs/<shortcode>; a closed job is a 404."""
    if not isinstance(data, dict) or str(data.get("shortcode") or "").upper() != target.job_id:
        raise ValueError("record does not match requested job")
    text, html = _sections([(None, data.get("description")), ("Requirements", data.get("requirements")),
                            ("Benefits", data.get("benefits"))])
    places = [p for p in (data.get("locations") or [data.get("location")]) if isinstance(p, dict) and not p.get("hidden")]
    names = [", ".join(_strings([p.get("city"), p.get("region"), p.get("country")])) for p in places]
    low, high = data.get("salary_from"), data.get("salary_to")
    salary = None
    if any(type(v) in (int, float) for v in (low, high)):
        salary = Salary(min=low if type(low) in (int, float) else None, max=high if type(high) in (int, float) else None,
                        currency=data.get("salary_currency_iso_code"), period=PERIOD.get(data.get("salary_frequency")),
                        source="structured")
    posted = parse_iso(data.get("published") if isinstance(data.get("published"), str) else None)
    return _posting(
        target, title=_title(data.get("title")), locations=[name for name in names if name],
        workplace_type=WORKABLE_WORKPLACE.get(data.get("workplace")) or ("remote" if data.get("remote") is True else "unknown"),
        employment_type=WORKABLE_EMPLOYMENT.get(data.get("type"), "unknown"),
        salary=salary or salary_from_description(text), posted_at=posted,
        posted_at_precision="exact" if posted else None, req_id=data.get("code") or None, description_text=text,
        description_html=html, extras={"country": _country(p.get("countryCode") for p in places)})


def amazon(data, target: Target) -> CommonPosting | None:
    """www.amazon.jobs/en/search.json?base_query=<id>: the job is the hit with that id_icims. A closed job
    leaves the index (and its page answers 404)."""
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("search must have a jobs array")
    job = next((j for j in data["jobs"] if isinstance(j, dict) and str(j.get("id_icims") or "") == target.job_id), None)
    if job is None:
        return None
    text, html = _sections([(None, job.get("description")), ("Basic Qualifications", job.get("basic_qualifications")),
                            ("Preferred Qualifications", job.get("preferred_qualifications"))])
    places = []
    for value in job.get("locations") or []:
        try:
            place = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            continue
        if isinstance(place, dict):
            places.append(place)
    workplaces = {workplace_of(p.get("type")) for p in places}
    try:
        posted = datetime.strptime(job.get("posted_date") or "", "%B %d, %Y").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        posted = None
    path = job.get("job_path")
    url = f"https://www.amazon.jobs{path}" if isinstance(path, str) and re.fullmatch(r"/[\w/-]+", path) else None
    return _posting(
        target, title=_title(job.get("title")),
        company=job.get("company_name") if isinstance(job.get("company_name"), str) else "Amazon",
        locations=_strings([p.get("normalizedLocation") or p.get("location") for p in places])
                  or _strings([job.get("normalized_location") or job.get("location")]),
        workplace_type=workplaces.pop() if len(workplaces) == 1 else "unknown",
        employment_type=employment_of(job.get("job_schedule_type")), salary=salary_from_description(text),
        posted_at=posted, posted_at_precision="day" if posted else None, req_id=target.job_id,
        description_text=text, description_html=html, extras={"country": _country(p.get("countryIso2a") for p in places)},
        **({"url": url} if url else {}))


def oracle(data, target: Target) -> CommonPosting | None:
    """<pod>.fa.<region>.oraclecloud.com recruitingCEJobRequisitionDetails, finder ById → {"items": [requisition]};
    a closed or unknown requisition answers an empty items list. The record never names the employer."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("record must have an items array")
    if not items:
        return None
    record = items[0]
    if not isinstance(record, dict) or str(record.get("Id") or "") != target.job_id:
        raise ValueError("record does not match requested job")
    text, html = _sections([(None, record.get("ExternalDescriptionStr")),
                            ("Responsibilities", record.get("ExternalResponsibilitiesStr")),
                            ("Qualifications", record.get("ExternalQualificationsStr")),
                            (None, record.get("OrganizationDescriptionStr")),
                            (None, record.get("CorporateDescriptionStr"))])
    others = [place for place in record.get("secondaryLocations") or [] if isinstance(place, dict)]
    posted = as_utc(parse_iso(record.get("ExternalPostedStartDate")
                              if isinstance(record.get("ExternalPostedStartDate"), str) else None))
    return _posting(
        target, title=_title(record.get("Title")),
        locations=_strings([record.get("PrimaryLocation"), *(place.get("Name") for place in others)]),
        workplace_type=workplace_of(record.get("WorkplaceType") if isinstance(record.get("WorkplaceType"), str)
                                    else None),
        employment_type=employment_of(record.get("JobSchedule") if isinstance(record.get("JobSchedule"), str)
                                      else None),
        salary=salary_from_description(text), posted_at=posted, posted_at_precision="exact" if posted else None,
        req_id=target.job_id, description_text=text, description_html=html,
        extras={"country": _country([record.get("PrimaryLocationCountry"),
                                     *(place.get("CountryCode") for place in others)])})


READERS = {"microsoft": microsoft, "eightfold": eightfold, "tiktok": tiktok, "jobright": jobright,
           "smartrecruiters": smartrecruiters, "workable": workable, "amazon": amazon, "oracle": oracle}
