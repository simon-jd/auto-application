from auto_app.core.discord import DiscordError


class Poster:
    sent = []
    closed = False

    def __init__(self, url):
        self.url = url

    def post(self, content=None, embeds=None):
        Poster.sent.append(content)

    def close(self):
        Poster.closed = True


def test_failures_are_posted_without_log_contents():
    from auto_app.ops.alert import send_failure

    Poster.sent = []
    code = send_failure("auto-app-worker.service", {"AUTO_APP_DISCORD_WEBHOOK_URL": "fixture-only", "SECRET": "hidden"},
                        discord_factory=Poster)
    assert code == 0
    assert "auto-app-worker.service" in Poster.sent[0] and "journalctl --user -u" in Poster.sent[0]
    assert "hidden" not in Poster.sent[0] and "fixture-only" not in Poster.sent[0]
    assert Poster.closed


def test_no_webhook_means_a_nonzero_exit():
    from auto_app.ops.alert import send_failure

    assert send_failure("x.service", {}, discord_factory=Poster) == 1


def test_unit_names_are_validated():
    from auto_app.ops.alert import send_failure

    Poster.sent = []
    for unit in ("x; rm -rf /", "-x.service", "../../x.service", "x\n.service"):
        assert send_failure(unit, {"AUTO_APP_DISCORD_WEBHOOK_URL": "fixture-only"}, discord_factory=Poster) == 1
    assert Poster.sent == []


def test_failed_post_returns_nonzero_and_closes():
    from auto_app.ops.alert import send_failure

    class Failing(Poster):
        def post(self, *args, **kwargs):
            raise DiscordError("fixture failure")

    Poster.closed = False
    assert send_failure("x.service", {"AUTO_APP_DISCORD_WEBHOOK_URL": "fixture-only"}, Failing) == 1
    assert Poster.closed


def test_service_failure_hooks_and_nightly_backup_timer():
    from pathlib import Path
    from auto_app.ingest.schedule_cli import build_systemd_unit

    root = Path(__file__).resolve().parents[2]
    for path in (root / "deploy").glob("*.service"):
        unit = path.read_text().split("[Service]", 1)[0]
        expected = "OnFailure=auto-app-alert@%n.service"
        assert (expected in unit) == (path.name != "auto-app-alert@.service"), path.name
    generated = build_systemd_unit(Path("/srv/code"), Path("/srv/runtime"), "/usr/bin/uv")
    assert "OnFailure=auto-app-alert@%n.service" in generated
    timer = (root / "deploy/auto-app-backup.timer").read_text()
    assert "03:30:00" in timer and "Persistent=true" in timer
