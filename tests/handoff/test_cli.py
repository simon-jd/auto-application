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
