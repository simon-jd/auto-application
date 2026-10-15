# tests/select/test_plan_screening.py
"""Jobs the screening rules excluded never cost a model run, unless asked for by id."""

from sqlalchemy import insert

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.select.engine import SelectOptions, plan
from auto_app.select.filters import load_filter
from tests.select.test_select import env  # noqa: F401 — the shared fixture


def test_plan_skips_screened_out_jobs_unless_asked(env):  # noqa: F811
    db, folder, jobs = env
    with db.engine.begin() as conn:
        conn.execute(insert(T.job_screenings).values(job_id=jobs["sales"], rules_version="t", facts_hash="h",
                                                     outcome="excluded", reasons=["not_cs_role"], signals={},
                                                     screened_at=utcnow()))
    flt = load_filter(folder / "ml")
    planned = {job for job, _ in plan(db, [flt], SelectOptions())}
    assert jobs["sales"] not in planned and jobs["ml"] in planned
    assert {job for job, _ in plan(db, [flt], SelectOptions(jobs=[jobs["sales"]]))} == {jobs["sales"]}
