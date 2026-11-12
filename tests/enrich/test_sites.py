"""Employer sites read only for enrichment: the records below mirror live responses (2026-11-02), trimmed."""

import json
from pathlib import Path

import httpx
import pytest

from auto_app.enrich.targets import target_for
from tests.enrich.test_fetch import fetcher

MS_URL = "https://apply.careers.microsoft.com/careers/job/1970393557050639"
MICROSOFT = {"status": 200, "error": {"message": "", "body": ""}, "data": {
    "id": 1970393557050639, "displayJobId": "200064642", "name": "Software Engineering IC2",
    "locations": ["United States, Washington, Redmond"], "standardizedLocations": ["Redmond, WA, US"],
    "postedTs": 1793645633, "department": "Software Engineering",
    "jobDescription": "<b>Overview</b><br><p>Build validation tools for Windows.</p><p>Software Engineering IC2 - "
                      "The typical base pay range for this role across the U.S. is USD $85,400 - $168,100 per year.</p>",
    "location": "United States, Washington, Redmond", "workLocationOption": "onsite", "atsJobId": "200064642",
    "publicUrl": MS_URL, "efcustomTextEmploymentType": ["Full-Time"]}}


def test_microsoft_position_api():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=MICROSOFT)
    result = fetcher(handler).fetch(target_for(MS_URL))
    assert str(seen[0].url) == ("https://apply.careers.microsoft.com/api/pcsx/position_details"
                                "?position_id=1970393557050639&domain=microsoft.com&hl=en")
    p = result.posting
    assert result.status == "ok" and p.title == "Software Engineering IC2" and p.company == "Microsoft"
    assert p.description_text.startswith("Overview\n\nBuild validation tools for Windows.")
    assert (p.employment_type, p.workplace_type, p.req_id) == ("full_time", "onsite", "200064642")
    assert p.locations == ["United States, Washington, Redmond"] and p.extras["country"] == "US"
    assert p.salary.min == 85400 and p.posted_at.year == 2026


def test_microsoft_closed_or_other_position():
    # A closed position answers 404 {"error": {"message": "Position not found"}}.
    missing = fetcher(lambda request: httpx.Response(404, json={"status": 404, "data": {}}))
    assert missing.fetch(target_for(MS_URL)).status == "gone"
    other = {**MICROSOFT, "data": {**MICROSOFT["data"], "id": 1970393557000000}}
    assert fetcher(lambda request: httpx.Response(200, json=other)).fetch(target_for(MS_URL)).status == "failed"


TT_ID = "7702517151362976053"


def tiktok_page(job_id=TT_ID, title="Product Solutions And Operations - Graduate - 2027 Start"):
    """The career sites render the job server-side (Next.js); scripts carry the same text again."""
    head = f'<title>{title}</title><meta property="og:title" content="{title}"/>' if title else ""
    job = "" if job_id is None else f"""
<div><p>Commercial product (ads)</p><h2 class="w-[609px] font-[700]">{title}</h2>
<div class="flex gap-1"><p class="font-[700]">Location<!-- -->:</p><p class="font-[400] ">San Jose</p></div>
<div class="flex gap-1"><p class="font-[700]">Employment Type<!-- -->:</p><p class="font-[400] ">Regular</p></div>
<div class="flex gap-1"><p class="font-[700]">Job Code<!-- -->:</p><p class="font-[400] ">A49572</p></div>
<a target="_blank" href="https://careers.tiktok.com/resume/{job_id}/apply"><button tracking-value="{job_id}">Apply to this job</button></a>
<div><p>Share this listing:</p></div>
<p class="text-[32px]">Responsibilities</p><p>The Commerce Ads Product team builds advertising products.</p>
<p class="text-[32px]">Qualifications</p><p>Minimum Qualification(s):<br/>- A Bachelor's degree in computer science</p>
<p class="text-[32px]">Job Information</p>
<div class="editor-content"><p>The base salary range for this position in the selected city is $76000 - $128000 annually.</p></div>
<a href="https://careers.tiktok.com/resume/{job_id}/apply"><button>Apply to this job</button></a></div>"""
    return f"""<!DOCTYPE html><html lang="en"><head>{head}</head><body>
<nav><a href="/teams">Teams</a><a href="/earlycareers">Early Careers</a></nav>{job}
<footer><p>Ready for a career at TikTok?</p><a href="/legal/privacy">Privacy Policy</a><p>@2026 TikTok</p></footer>
<script>self.__next_f.push([1,"Privacy Policy Responsibilities"])</script></body></html>"""


def test_tiktok_job_page():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=tiktok_page())
    result = fetcher(handler).fetch(target_for(f"https://lifeattiktok.com/search/{TT_ID}?spread=5MWH5CQ"))
    assert str(seen[0].url) == f"https://lifeattiktok.com/search/{TT_ID}"
    assert seen[0].headers["accept"].startswith("text/html")
    p = result.posting
    assert result.status == "ok" and p.title == "Product Solutions And Operations - Graduate - 2027 Start"
    assert (p.company, p.locations, p.employment_type, p.req_id) == ("TikTok", ["San Jose"], "full_time", "A49572")
    assert p.description_text.startswith("Responsibilities\n\nThe Commerce Ads Product team")
    assert "A Bachelor's degree in computer science" in p.description_text
    assert "Privacy Policy" not in p.description_text and "Apply to this job" not in p.description_text
    assert p.salary.min == 76000


def test_bytedance_links_read_the_joinbytedance_page():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=tiktok_page("7670374385110599989"))
    result = fetcher(handler).fetch(target_for("https://jobs.bytedance.com/en/position/7670374385110599989/detail"))
    assert seen == ["https://joinbytedance.com/search/7670374385110599989"]
    assert result.status == "ok" and result.posting.company == "ByteDance"


def test_tiktok_missing_or_other_job():
    # An unknown id renders the site without a job: no title and no apply link.
    gone = fetcher(lambda request: httpx.Response(200, text=tiktok_page(None, None)))
    assert gone.fetch(target_for(f"https://lifeattiktok.com/search/{TT_ID}")).status == "gone"
    other = fetcher(lambda request: httpx.Response(200, text=tiktok_page("7517108164819454216")))
    assert other.fetch(target_for(f"https://lifeattiktok.com/search/{TT_ID}")).status == "failed"
    blocked = fetcher(lambda request: httpx.Response(200, text="<html><body>Access denied</body></html>"))
    assert blocked.fetch(target_for(f"https://lifeattiktok.com/search/{TT_ID}")).status == "failed"


JR_ID = "68cb63c89e3a822f5d2459c7"
JR_URL = f"https://jobright.ai/jobs/info/{JR_ID}"
JOBRIGHT = {"jobResult": {
    "jobId": JR_ID, "jobTitle": "Software Developer", "jobLocation": "New York, NY", "jobLocations": ["New York, NY"],
    "isRemote": False, "workModel": "Onsite", "publishTime": "2026-10-08 18:33:35", "salaryDesc": "$225K/yr - $225K/yr",
    "minSalary": 225000, "maxSalary": 225000, "employmentType": "Full-time", "countryCode": "US", "isDeleted": False,
    "jobSummary": "The D. E. Shaw Group is seeking software developers with quantitative skills.",
    "coreResponsibilities": ["Formulating statistical models", "Developing distributed systems"],
    "qualifications": {"mustHave": ["Excellent analytical skills"], "preferredHave": ["Trading experience"]},
    "benefitsSummaries": ["401(k) contribution matching"]},
    "companyResult": {"companyId": "6745500", "companyName": "The D. E. Shaw Group"}}


def jobright_page(source):
    data = {"props": {"pageProps": {"dataSource": source, "logined": False, "jobHashedId": JR_ID}},
            "page": "/jobs/info/[id]", "query": {"id": JR_ID}}
    return (f'<html><head><title>Software Developer</title></head><body><div id="__next"></div>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>')


def test_jobright_logged_out_page():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=jobright_page(JOBRIGHT))
    result = fetcher(handler).fetch(target_for(JR_URL + "?utm_source=feed"))
    assert str(seen[0].url) == JR_URL and seen[0].headers["accept"].startswith("text/html")
    p = result.posting
    assert result.status == "ok" and (p.title, p.company) == ("Software Developer", "The D. E. Shaw Group")
    assert p.description_text.startswith("The D. E. Shaw Group is seeking software developers")
    for text in ("Responsibilities", "- Formulating statistical models", "Qualifications", "- Excellent analytical skills",
                 "Preferred", "- Trading experience", "Benefits", "- 401(k) contribution matching"):
        assert text in p.description_text
    assert (p.employment_type, p.workplace_type, p.locations) == ("full_time", "onsite", ["New York, NY"])
    assert (p.salary.min, p.salary.max, p.salary.period) == (225000, 225000, "year") and p.extras["country"] == "US"


def test_jobright_deleted_other_or_handed_off_listing():
    deleted = {**JOBRIGHT, "jobResult": {**JOBRIGHT["jobResult"], "isDeleted": True}}
    assert fetcher(lambda r: httpx.Response(200, text=jobright_page(deleted))).fetch(target_for(JR_URL)).status == "gone"
    other = {**JOBRIGHT, "jobResult": {**JOBRIGHT["jobResult"], "jobId": "698c183c0f6f7e7a2ce7aad3"}}
    assert fetcher(lambda r: httpx.Response(200, text=jobright_page(other))).fetch(target_for(JR_URL)).status == "failed"
    assert fetcher(lambda r: httpx.Response(200, text="<html></html>")).fetch(target_for(JR_URL)).status == "failed"
    # Some listings send logged-out visitors to another job board; that page is not Jobright's.
    elsewhere = fetcher(lambda r: httpx.Response(307, headers={"location": "https://www.ziprecruiter.com/kn/x"}))
    result = elsewhere.fetch(target_for(JR_URL))
    assert result.status == "unsupported" and result.posting is None


SR_URL = "https://jobs.smartrecruiters.com/AbbVie/3743990016568546-associate-device-software-engineer?oga=true"
SMARTRECRUITERS = {
    "id": "3743990016568546", "name": "Associate Device Software Engineer", "uuid": "963f0f0c-5704-4e22-ba54-ae16e5d41d46",
    "refNumber": "R00152", "company": {"name": "AbbVie", "identifier": "AbbVie"},
    "location": {"city": "Mettawa", "region": "IL", "country": "us", "remote": False, "hybrid": True,
                 "fullLocation": "Mettawa, IL, United States"},
    "releasedDate": "2026-11-02T20:41:21.772Z", "active": True, "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
    "postingUrl": "https://jobs.smartrecruiters.com/AbbVie/3743990016568546-associate-device-software-engineer",
    "jobAd": {"sections": {
        "companyDescription": {"title": "Company Description", "text": "<p>AbbVie discovers medicines.</p>"},
        "jobDescription": {"title": "Job Description", "text": "<p>Write device software in C++.</p>"},
        "qualifications": {"title": "Qualifications", "text": "<ul><li>BS in Computer Science</li></ul>"},
        "additionalInformation": {"title": "Additional Information", "text": ""}}}}


def test_smartrecruiters_posting_api():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=SMARTRECRUITERS)
    result = fetcher(handler).fetch(target_for(SR_URL))
    assert seen == ["https://api.smartrecruiters.com/v1/companies/AbbVie/postings/3743990016568546"]
    p = result.posting
    assert result.status == "ok" and (p.title, p.company, p.req_id) == ("Associate Device Software Engineer", "AbbVie", "R00152")
    assert p.description_text == ("Company Description\nAbbVie discovers medicines.\n\nJob Description\n"
                                  "Write device software in C++.\n\nQualifications\n- BS in Computer Science")
    assert (p.employment_type, p.workplace_type, p.locations) == ("full_time", "hybrid", ["Mettawa, IL, United States"])
    assert p.extras["country"] == "US"


def test_smartrecruiters_publication_link_and_inactive_or_other_posting():
    uuid_url = f"https://jobs.smartrecruiters.com/oneclick-ui/company/AbbVie/publication/{SMARTRECRUITERS['uuid']}"
    assert fetcher(lambda r: httpx.Response(200, json=SMARTRECRUITERS)).fetch(target_for(uuid_url)).status == "ok"
    inactive = {**SMARTRECRUITERS, "active": False}
    assert fetcher(lambda r: httpx.Response(200, json=inactive)).fetch(target_for(SR_URL)).status == "gone"
    other = {**SMARTRECRUITERS, "id": "1", "uuid": "00000000-0000-0000-0000-000000000000"}
    assert fetcher(lambda r: httpx.Response(200, json=other)).fetch(target_for(SR_URL)).status == "failed"


WK_URL = "https://apply.workable.com/amax-1/j/C8FD3B365F/apply"
WORKABLE = {
    "id": 6242006, "shortcode": "C8FD3B365F", "title": "Junior System Engineer", "remote": False,
    "location": {"city": "Fremont", "country": "United States", "countryCode": "US", "region": "California"},
    "locations": [{"city": "Fremont", "country": "United States", "countryCode": "US", "hidden": False,
                   "region": "California"}],
    "state": "published", "code": "SE-12", "published": "2026-11-02T00:00:00.000Z", "type": "full", "workplace": "on_site",
    "description": "<p>Build and test servers.</p>", "requirements": "<ul><li>Linux</li></ul>", "benefits": "<p>Medical</p>",
    "salary_from": 80000, "salary_to": 95000, "salary_currency_iso_code": "USD", "salary_frequency": "year"}


def test_workable_job_api():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=WORKABLE)
    result = fetcher(handler).fetch(target_for(WK_URL))
    assert seen == ["https://apply.workable.com/api/v2/accounts/amax-1/jobs/C8FD3B365F"]
    p = result.posting
    assert result.status == "ok" and (p.title, p.req_id) == ("Junior System Engineer", "SE-12")
    assert p.description_text == "Build and test servers.\n\nRequirements\n- Linux\n\nBenefits\nMedical"
    assert (p.employment_type, p.workplace_type, p.locations) == ("full_time", "onsite", ["Fremont, California, United States"])
    assert (p.salary.min, p.salary.max, p.salary.currency, p.salary.period) == (80000, 95000, "USD", "year")
    assert p.extras["country"] == "US"


def test_workable_missing_or_other_job():
    # Workable answers 404 "Job not found" for a closed or unknown shortcode.
    assert fetcher(lambda r: httpx.Response(404, text="Job not found")).fetch(target_for(WK_URL)).status == "gone"
    other = {**WORKABLE, "shortcode": "5840ECEB50"}
    assert fetcher(lambda r: httpx.Response(200, json=other)).fetch(target_for(WK_URL)).status == "failed"


AMZ_URL = "https://www.amazon.jobs/en/jobs/10593902/software-development-engineer-database-early-career-2027-us"
AMAZON_JOB = {
    "id_icims": "10593902", "title": "Software Development Engineer (Database) Early Career- 2027 (US)",
    "company_name": "Amazon.com Services LLC", "location": "US, WA, Seattle", "normalized_location": "Seattle, Washington, USA",
    "locations": [json.dumps({"normalizedLocation": "Boston, Massachusetts, USA", "countryIso2a": "US", "type": "ONSITE"}),
                  json.dumps({"normalizedLocation": "Seattle, Washington, USA", "countryIso2a": "US", "type": "ONSITE"})],
    "description": "There’s a universe of opportunity at Amazon.<br/><br/>Build databases.",
    "basic_qualifications": "- Experience with at least one general-purpose programming language",
    "preferred_qualifications": "- Previous technical internship(s)<br/>- Knowledge of database internals",
    "job_schedule_type": "full-time", "posted_date": "October 30, 2026",
    "job_path": "/en/jobs/10593902/software-development-engineer-database-early-career-2027-us",
    "url_next_step": "https://account.amazon.jobs/jobs/10593902/apply"}


def test_amazon_job_from_its_search_index():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"error": None, "hits": 2, "jobs": [{**AMAZON_JOB, "id_icims": "105939020"},
                                                                            AMAZON_JOB]})
    result = fetcher(handler).fetch(target_for(AMZ_URL))
    assert seen == ["https://www.amazon.jobs/en/search.json?base_query=10593902&result_limit=10"]
    p = result.posting
    assert result.status == "ok" and p.title == AMAZON_JOB["title"] and p.company == "Amazon.com Services LLC"
    assert p.description_text.startswith("There’s a universe of opportunity at Amazon.\n\nBuild databases.")
    assert "Basic Qualifications\n- Experience with at least one" in p.description_text
    assert "Preferred Qualifications\n- Previous technical internship(s)\n- Knowledge" in p.description_text
    assert p.locations == ["Boston, Massachusetts, USA", "Seattle, Washington, USA"] and p.extras["country"] == "US"
    assert (p.employment_type, p.req_id, p.posted_at.day) == ("full_time", "10593902", 30)


def test_amazon_job_missing_from_the_index_is_gone():
    # A closed job's page answers 404 and the index no longer lists it.
    empty = fetcher(lambda r: httpx.Response(200, json={"error": None, "hits": 0, "jobs": []}))
    assert empty.fetch(target_for(AMZ_URL)).status == "gone"
    broken = fetcher(lambda r: httpx.Response(200, json={"error": "search unavailable"}))
    assert broken.fetch(target_for(AMZ_URL)).status == "failed"


@pytest.mark.parametrize("url", [MS_URL, f"https://lifeattiktok.com/search/{TT_ID}", JR_URL, SR_URL, WK_URL, AMZ_URL])
def test_new_sites_are_paced_and_counted_like_the_others(url):
    f = fetcher(lambda request: httpx.Response(500))
    assert f.fetch(target_for(url)).status == "failed" and f.request_count == 1 and f.request_needed(target_for(url)) == 1


FIXTURES = Path(__file__).parents[1] / "fixtures"
QC_URL = "https://qualcomm.eightfold.ai/careers/job/446721159325?domain=qualcomm.com"


def recorded(name):
    return json.loads((FIXTURES / name).read_text())


def test_eightfold_tenant_position_api():
    # Recorded from Qualcomm's tenant (2026-11-06), the site's videos and perks cut out.
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=recorded("eightfold/qualcomm_detail.json"))
    result = fetcher(handler).fetch(target_for(QC_URL))
    assert str(seen[0].url) == ("https://qualcomm.eightfold.ai/api/pcsx/position_details"
                                "?position_id=446721159325&domain=qualcomm.com&hl=en")
    p = result.posting
    assert result.status == "ok" and p.title == "Senior Embedded Software Engineer" and p.company is None
    assert p.description_text.startswith("Company:\nQualcomm Technologies, Inc.")
    assert "Location Technology Team" in p.description_text and p.description_html
    assert (p.workplace_type, p.req_id, p.extras["country"]) == ("onsite", "3096868", "US")
    assert p.locations == ["Santa Clara, California, United States of America"]
    assert p.url == QC_URL and p.posted_at.year == 2026


def test_eightfold_closed_or_other_position():
    missing = fetcher(lambda request: httpx.Response(404, json={"status": 404, "data": {}}))
    assert missing.fetch(target_for(QC_URL)).status == "gone"
    other = recorded("eightfold/qualcomm_detail.json")
    other["data"]["id"] = 446721159329
    assert fetcher(lambda request: httpx.Response(200, json=other)).fetch(target_for(QC_URL)).status == "failed"


ORACLE_URL = "https://efds.fa.em5.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/73674"


def test_oracle_requisition_details():
    # Recorded from Ford's site (2026-11-06). Lookup values follow the requisition's own locale (pt-BR here)
    # while its text is English; the import's English location and country are kept (enrich/service.py).
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=recorded("oracle/ford_detail.json"))
    result = fetcher(handler).fetch(target_for(ORACLE_URL))
    assert str(seen[0].url) == ("https://efds.fa.em5.oraclecloud.com/hcmRestApi/resources/latest/"
                                "recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                                "&finder=ById;Id=%2273674%22,siteNumber=CX_1")
    p = result.posting
    assert result.status == "ok" and p.title == "IVI & Audio Hardware Quality Supervisor" and p.company is None
    assert p.description_text.startswith("The Quality Supervisor – Electronics Platforms Warranty & Customer Quality")
    assert "\n\nResponsibilities\n" in p.description_text and "\n\nQualifications\n" in p.description_text
    assert (p.req_id, p.workplace_type, p.extras["country"]) == ("73674", "hybrid", "US")
    assert p.employment_type == "unknown"  # "Período integral"
    assert p.url == ORACLE_URL and p.posted_at.isoformat() == "2026-11-06T00:00:12+00:00"


def test_oracle_closed_or_other_requisition():
    closed = fetcher(lambda request: httpx.Response(200, json={"items": [], "count": 0, "hasMore": False}))
    assert closed.fetch(target_for(ORACLE_URL)).status == "gone"
    other = recorded("oracle/ford_detail.json")
    other["items"][0]["Id"] = "73677"
    assert fetcher(lambda request: httpx.Response(200, json=other)).fetch(target_for(ORACLE_URL)).status == "failed"
    assert fetcher(lambda request: httpx.Response(200, json={"count": 0})).fetch(target_for(ORACLE_URL)).status == "failed"
