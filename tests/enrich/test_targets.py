import pytest

from auto_app.enrich.targets import Target, target_for

UUID = "6ed76ce8-1111-2222-3333-444455556666"


@pytest.mark.parametrize("url, expected", [
    ("https://job-boards.greenhouse.io/acme/jobs/4012345?utm_source=feed",
     Target("greenhouse", "acme", "4012345", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345")),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345",
     Target("greenhouse", "acme", "4012345", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345")),
    (f"https://jobs.lever.co/acme/{UUID}/apply",
     Target("lever", "acme", UUID, f"https://api.lever.co/v0/postings/acme/{UUID}")),
    (f"https://jobs.eu.lever.co/acme/{UUID}",
     Target("lever", "acme", UUID, f"https://api.eu.lever.co/v0/postings/acme/{UUID}")),
    (f"https://jobs.ashbyhq.com/Acme/{UUID}/application",
     Target("ashby", "acme", UUID, "https://api.ashbyhq.com/posting-api/job-board/Acme")),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-TX-Austin/Software-Engineer_JR2026343/apply",
     Target("workday", "nvidia/nvidiaexternalcareersite", "JR2026343",
            "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/US-TX-Austin/Software-Engineer_JR2026343")),
])
def test_supported_platform_links(url, expected):
    assert target_for(url) == expected


@pytest.mark.parametrize("url", [
    None, "", "https://acme.com/careers?gh_jid=4012345", "https://careers.oracle.com/jobs/123",
    "https://wd3.myworkdaysite.com/recruiting/acme/External/job/X_R1", "https://jobs.lever.co/acme/not-a-uuid",
    "https://job-boards.greenhouse.io/acme", "https://acme.wd1.myworkdayjobs.com/External",
    "http://job-boards.greenhouse.io/acme/jobs/1", "https://job-boards.greenhouse.io:8080/acme/jobs/1",
    "https://secret@job-boards.greenhouse.io/acme/jobs/1", "https://job-boards.greenhouse.io.evil.test/acme/jobs/1",
    "https://boards.greenhouse.io/embed/job_app?for=../../internal&token=1",
    "https://boards.greenhouse.io/embed/job_app?for=acme%3Fsecret&token=1",
    "https://boards.greenhouse.io/embed/job_app?for=acme&for=other&token=1",
    "https://job-boards.greenhouse.io/acme/jobs/1/../../private", "https://[invalid/acme/jobs/1",
    "https://job-boards.greenhouse.io/acme/jobs/1\n", "https://acme.myworkdayjobs.com/External/job/X_R1",
    "https://acme.wd1.myworkdayjobs.com/External/job/%2E%2E/X_R1",
    "https://acme.wd1.myworkdayjobs.com/External/job/A%2FB_R1",
    "https://acme.wd1.myworkdayjobs.com/External/job/%252E%252E/X_R1",
    "https://acme.wd1.myworkdayjobs.com/External/job/No-Requisition",
])
def test_unsupported_or_unsafe_links_have_no_target(url):
    assert target_for(url) is None
