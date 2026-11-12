import pytest

from auto_app.enrich.targets import Target, target_for, valid_target

UUID = "6ed76ce8-1111-2222-3333-444455556666"
SR_UUID = "f16373b6-3eea-4dcd-b77e-586b584a8b27"


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


@pytest.mark.parametrize("url, expected", [
    # EU boards are read through the global boards API: boards-api.eu.greenhouse.io does not exist.
    ("https://job-boards.eu.greenhouse.io/agency/jobs/4738026101",
     Target("greenhouse", "agency", "4738026101", "https://boards-api.greenhouse.io/v1/boards/agency/jobs/4738026101")),
    ("https://boards.eu.greenhouse.io/think-cell/jobs/4844701101?gh_src=feed",
     Target("greenhouse", "think-cell", "4844701101",
            "https://boards-api.greenhouse.io/v1/boards/think-cell/jobs/4844701101")),
])
def test_greenhouse_eu_boards(url, expected):
    assert target_for(url) == expected


@pytest.mark.parametrize("url, expected", [
    (f"https://jobs.ashbyhq.com/jerry.ai/{UUID}",
     Target("ashby", "jerry.ai", UUID, "https://api.ashbyhq.com/posting-api/job-board/jerry.ai")),
    (f"https://jobs.ashbyhq.com/Spectral%20Labs/{UUID}/application?embed=true",
     Target("ashby", "spectral labs", UUID, "https://api.ashbyhq.com/posting-api/job-board/Spectral%20Labs")),
])
def test_ashby_board_names_with_dots_and_spaces(url, expected):
    assert target_for(url) == expected


@pytest.mark.parametrize("url, expected", [
    ("https://apply.careers.microsoft.com/careers/job/1970393557050639?domain=microsoft.com&src=jobright",
     Target("microsoft", "microsoft.com", "1970393557050639",
            "https://apply.careers.microsoft.com/api/pcsx/position_details"
            "?position_id=1970393557050639&domain=microsoft.com&hl=en")),
    ("https://lifeattiktok.com/search/7702517151362976053?spread=5MWH5CQ",
     Target("tiktok", "lifeattiktok.com", "7702517151362976053", "https://lifeattiktok.com/search/7702517151362976053")),
    ("https://joinbytedance.com/search/7667894766036322565",
     Target("tiktok", "joinbytedance.com", "7667894766036322565",
            "https://joinbytedance.com/search/7667894766036322565")),
    # jobs.bytedance.com redirects its English job pages to joinbytedance.com.
    ("https://jobs.bytedance.com/en/position/7670374385110599989/detail",
     Target("tiktok", "joinbytedance.com", "7670374385110599989",
            "https://joinbytedance.com/search/7670374385110599989")),
    ("https://jobright.ai/jobs/info/68cb63c89e3a822f5d2459c7?utm_source=feed",
     Target("jobright", "jobright.ai", "68cb63c89e3a822f5d2459c7", "https://jobright.ai/jobs/info/68cb63c89e3a822f5d2459c7")),
    ("https://www.jobright.ai/jobs/info/68cb63c89e3a822f5d2459c7",
     Target("jobright", "jobright.ai", "68cb63c89e3a822f5d2459c7", "https://jobright.ai/jobs/info/68cb63c89e3a822f5d2459c7")),
    ("https://jobs.smartrecruiters.com/BCforward3/101589991",
     Target("smartrecruiters", "BCforward3", "101589991",
            "https://api.smartrecruiters.com/v1/companies/BCforward3/postings/101589991")),
    ("https://jobs.smartrecruiters.com/ServiceNow/744000153279380-software-engineer-new-grad-?oga=true",
     Target("smartrecruiters", "ServiceNow", "744000153279380",
            "https://api.smartrecruiters.com/v1/companies/ServiceNow/postings/744000153279380")),
    (f"https://jobs.smartrecruiters.com/oneclick-ui/company/PreludeSysIndiaLtd/publication/{SR_UUID}?dcr_ci=x",
     Target("smartrecruiters", "PreludeSysIndiaLtd", SR_UUID,
            f"https://api.smartrecruiters.com/v1/companies/PreludeSysIndiaLtd/postings/{SR_UUID}")),
    ("https://apply.workable.com/tickpick/j/5840ECEB50/apply",
     Target("workable", "tickpick", "5840ECEB50", "https://apply.workable.com/api/v2/accounts/tickpick/jobs/5840ECEB50")),
    ("https://apply.workable.com/oxford-quantum-circuits-8/j/5A9494E612",
     Target("workable", "oxford-quantum-circuits-8", "5A9494E612",
            "https://apply.workable.com/api/v2/accounts/oxford-quantum-circuits-8/jobs/5A9494E612")),
    ("https://www.amazon.jobs/en/jobs/10593902/software-development-engineer-early-career-2027-us?cmpid=X",
     Target("amazon", "amazon.jobs", "10593902",
            "https://www.amazon.jobs/en/search.json?base_query=10593902&result_limit=10")),
    ("https://amazon.jobs/en/jobs/10408763/software-development-engineer-2026",
     Target("amazon", "amazon.jobs", "10408763", "https://www.amazon.jobs/en/search.json?base_query=10408763&result_limit=10")),
    ("https://www.amazon.jobs/jobs/10519658/apply",
     Target("amazon", "amazon.jobs", "10519658", "https://www.amazon.jobs/en/search.json?base_query=10519658&result_limit=10")),
    # eightfold-board links: the tenant answers for the career site's domain.
    ("https://qualcomm.eightfold.ai/careers/job/446721159325?domain=qualcomm.com&src=feed",
     Target("eightfold", "qualcomm.eightfold.ai/qualcomm.com", "446721159325",
            "https://qualcomm.eightfold.ai/api/pcsx/position_details?position_id=446721159325&domain=qualcomm.com&hl=en")),
    # oracle-board links: the requisition's detail through the finder.
    ("https://efds.fa.em5.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/73674?utm_medium=jobshare",
     Target("oracle", "efds.fa.em5.oraclecloud.com/CX_1", "73674",
            "https://efds.fa.em5.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            "?expand=all&onlyData=true&finder=ById;Id=%2273674%22,siteNumber=CX_1")),
    ("https://ibqbjb.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Honeywell/job/req480976",
     Target("oracle", "ibqbjb.fa.ocs.oraclecloud.com/Honeywell", "req480976",
            "https://ibqbjb.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            "?expand=all&onlyData=true&finder=ById;Id=%22req480976%22,siteNumber=Honeywell")),
    ("https://ibqbjb.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en-US/sites/Honeywell/job/147267/",
     Target("oracle", "ibqbjb.fa.ocs.oraclecloud.com/Honeywell", "147267",
            "https://ibqbjb.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            "?expand=all&onlyData=true&finder=ById;Id=%22147267%22,siteNumber=Honeywell")),
])
def test_employer_sites_without_a_collector(url, expected):
    assert target_for(url) == expected
    assert valid_target(expected)


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
    f"https://jobs.ashbyhq.com/.hidden/{UUID}", f"https://jobs.ashbyhq.com/acme%2Fother/{UUID}",
    f"https://jobs.ashbyhq.com/acme%20/{UUID}",
    "https://apply.careers.microsoft.com/careers/job/abc", "https://apply.careers.microsoft.com/careers/search",
    "https://lifeattiktok.com/search", "https://lifeattiktok.com/search/abc",
    "https://jobs.bytedance.com/experienced/position/7670374385110599989/detail",
    "https://jobright.ai/jobs/info/not-an-id", "https://jobright.ai/jobs/recommend",
    "https://jobs.smartrecruiters.com/Acme", "https://jobs.smartrecruiters.com/Acme/software-engineer",
    "https://jobs.smartrecruiters.com/external-referrals/company/Acme/publication/" + SR_UUID,
    "https://apply.workable.com/acme/", "https://apply.workable.com/acme/j/", "https://apply.workable.com/acme/j/A%2FB",
    "https://www.amazon.jobs/en/teams/aws", "https://www.amazon.jobs/en/jobs/not-a-number/x",
    "https://qualcomm.eightfold.ai/careers/job/446721159325", "https://qualcomm.eightfold.ai/careers/job/x?domain=q.com",
    "https://qualcomm.eightfold.ai/careers/job/1?domain=q.com&domain=r.com",
    "https://qualcomm.eightfold.ai/careers/job/1?domain=Q_C", "https://eightfold.ai/careers/job/1?domain=q.com",
    "https://efds.fa.em5.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/-abc",
    "https://efds.fa.em5.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions",
    "https://efds.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1",
    "https://efds.fa.em5.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX-1/job/1",
])
def test_unsupported_or_unsafe_links_have_no_target(url):
    assert target_for(url) is None


@pytest.mark.parametrize("target", [
    Target("eightfold", "qualcomm.eightfold.ai", "1", "https://qualcomm.eightfold.ai/api/pcsx/position_details"),
    Target("eightfold", "evil.example/qualcomm.com", "1",
           "https://evil.example/api/pcsx/position_details?position_id=1&domain=qualcomm.com&hl=en"),
    Target("oracle", "efds.fa.em5.oraclecloud.com", "1", "https://efds.fa.em5.oraclecloud.com/hcmRestApi"),
    Target("oracle", "evil.example/CX_1", "1", "https://evil.example/hcmRestApi/resources/latest/x"),
    Target("oracle", "efds.fa.em5.oraclecloud.com/CX_1", "1,siteNumber=X", "https://efds.fa.em5.oraclecloud.com/x"),
])
def test_forged_site_targets_are_refused(target):
    assert valid_target(target) is False
