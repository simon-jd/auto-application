from tests.track.test_updates import NOW, mail, store


def test_control_reviews_updates(store):
    from auto_app.agent.control import ControlService
    from auto_app.track.updates import record_update
    from tests.imported_jobs import job_id_of
    content = mail("Other <jobs@other.com>", "Your application", "We have decided to move forward with other candidates.")
    first = record_update(store, "mail", "one", content, NOW)
    second = record_update(store, "mail", "two", content, NOW)
    control = ControlService(store.db_path)
    assert len(control.unmatched_updates()) == 2
    assert control.assign_update(first["update_id"], job_id_of(store, "Backend Engineer"))["status"] == "matched"
    assert control.dismiss_update(second["update_id"])["status"] == "dismissed"
    assert control.unmatched_updates() == []
