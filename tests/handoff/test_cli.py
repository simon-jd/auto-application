import json

from typer.testing import CliRunner

from auto_app.cli import app
from tests.handoff.test_service import env  # noqa: F401


def run(home, *args):
    result = CliRunner().invoke(app, ["handoff", *args], env={"AUTO_APP_HOME": str(home)})
    return result.exit_code, result.output


def test_queue_claim_record_round_trip(env):  # noqa: F811
    home, ids = env
    code, out = run(home, "queue")
    assert code == 0 and json.loads(out)["items"]
    code, out = run(home, "claim", str(ids["ML Engineer"]), "--agent", "my-agent")
    token = json.loads(out)["token"]
    code, out = run(home, "record", token, "needs_user", "--note", "email verification")
    assert code == 0 and json.loads(out)["status"] == "needs_input"
    code, out = run(home, "record", token, "failed")
    assert code == 1 and "claim" in out


def test_cli_packet_account_resume_release_and_requeue(env):
    home, ids = env
    (home / 'resumes').mkdir()
    resume = home / 'resumes' / 'fictional.pdf'
    resume.write_bytes(b'%PDF-fixture')
    code, output = run(home, 'resume', str(ids['ML Engineer']), str(resume))
    assert code == 0 and json.loads(output)['sha256']
    code, output = run(home, 'account', 'acme.example.com', 'alex@example.com', '--existing')
    assert code == 0 and json.loads(output)['created_by_agent'] is False
    code, output = run(home, 'claim', str(ids['ML Engineer']), '--agent', 'fictional-agent')
    assert code == 0
    token = json.loads(output)['token']
    code, output = run(home, 'packet', token)
    assert code == 0 and json.loads(output)['resumes'][0]['path'] == str(resume)
    code, output = run(home, 'record', token, 'blocked')
    assert code == 0 and json.loads(output)['status'] == 'action_needed'
    code, output = run(home, 'requeue', str(ids['ML Engineer']))
    assert code == 0 and json.loads(output)['status'] == 'queued'
    code, output = run(home, 'claim', str(ids['ML Engineer']), '--agent', 'fictional-agent')
    assert code == 0
    token = json.loads(output)['token']
    code, output = run(home, 'release', token)
    assert code == 0 and json.loads(output)['released'] is True


def test_cli_questions_answers_and_hold(env, tmp_path):  # noqa: F811
    home, ids = env
    job = ids["ML Engineer"]
    token = json.loads(run(home, "claim", str(job), "--agent", "my-agent")[1])["token"]
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps([{"text": "Why us?", "limit": "200 words"}]))
    code, out = run(home, "questions", token, str(questions))
    assert code == 0 and json.loads(out)["status"] == "needs_answers"
    code, out = run(home, "answers-needed")
    assert code == 0 and [i["job_id"] for i in json.loads(out)["items"]] == [job]
    code, out = run(home, "answers-ready", str(job))
    assert code == 0 and json.loads(out)["status"] == "queued"
    token = json.loads(run(home, "claim", str(job), "--agent", "my-agent")[1])["token"]
    code, out = run(home, "hold", token)
    assert code == 0 and json.loads(out)["status"] == "ready"
    code, out = run(home, "questions", token, str(tmp_path / "missing.json"))
    assert code == 1 and "questions file" in out
