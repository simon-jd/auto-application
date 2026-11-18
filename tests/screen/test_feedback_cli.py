"""`auto-app screen-feedback`: what the daily feedback summary (deploy/screening-feedback.sh) runs. JSON out, always."""

import json

from typer.testing import CliRunner

from auto_app.cli import app
from auto_app.screen.feedback import ScreeningFeedback
from tests.screen.test_feedback import NOW, jobs  # noqa: F401  (jobs is a fixture)


def run(db, *args):
    result = CliRunner().invoke(app, ["screen-feedback", *args, "--db", str(db)])
    return result.exit_code, json.loads(result.output)


def test_due_list_propose_and_close(jobs):  # noqa: F811
    db, ids = jobs
    feedback = ScreeningFeedback(db)
    one = feedback.record(ids["Field Service Associate"], "现场服务，不是软件岗", now=NOW)["feedback_id"]
    two = feedback.record(ids["Platform Engineer"], "方向不对", now=NOW)["feedback_id"]
    assert run(db, "due") == (0, {"due": False, "pending": 2})
    code, listed = run(db, "list")
    assert code == 0 and [i["feedback_id"] for i in listed["items"]] == [one, two]
    assert "extra_non_cs_terms" in listed["rules"]["rules"] and listed["rules"]["revision"]
    code, proposed = run(db, "propose", "--changes", json.dumps({"extra_non_cs_terms": ["field service"]}),
                         "--ids", str(one), "--note", "排除 field service")
    assert code == 0 and proposed["preview"]["newly_excluded"] == 1 and proposed["attached"] == 1
    assert feedback.open_proposals() == [proposed["change_id"]]
    code, closed = run(db, "close", "--ids", str(two), "--note", "关键词分不出方向")
    assert code == 0 and closed == {"closed": 1}
    assert run(db, "due") == (0, {"due": False, "pending": 0})


def test_a_failed_attach_leaves_no_proposal_behind(jobs):  # noqa: F811
    db, ids = jobs
    feedback = ScreeningFeedback(db)
    one = feedback.record(ids["Software Engineer"], "a", now=NOW)["feedback_id"]
    feedback.close([one], "done", now=NOW)
    code, out = run(db, "propose", "--changes", json.dumps({"extra_non_cs_terms": ["x"]}), "--ids", str(one))
    assert code == 1 and "not pending" in out["error"]
    assert not list(feedback.control.proposals.glob("*.json"))


def test_bad_input_is_an_error_in_json(jobs):  # noqa: F811
    db, _ = jobs
    assert run(db, "propose", "--changes", "not json", "--ids", "1")[0] == 1
    assert run(db, "propose", "--changes", json.dumps({"no_such_rule": True}), "--ids", "1")[0] == 1
    assert run(db, "close", "--ids", "x", "--note", "n")[0] == 1


def test_preview_shows_the_effect_without_holding_the_reasons(jobs):  # noqa: F811
    db, ids = jobs
    feedback = ScreeningFeedback(db)
    one = feedback.record(ids["Field Service Associate"], "现场服务", now=NOW)["feedback_id"]
    code, preview = run(db, "preview", "--changes", json.dumps({"extra_non_cs_terms": ["field"]}))
    assert code == 0 and preview["preview"]["newly_excluded"] >= 1 and "change_id" not in preview
    assert not list(feedback.control.proposals.glob("*.json"))  # nothing saved, nothing to Apply
    assert [i["feedback_id"] for i in feedback.pending()] == [one]  # still free for propose or close
    code, proposed = run(db, "propose", "--changes", json.dumps({"extra_non_cs_terms": ["field service"]}),
                         "--ids", str(one))
    assert code == 0 and proposed["attached"] == 1
