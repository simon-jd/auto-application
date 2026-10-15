"""Import records keep a description's fingerprint, not a second copy of its text."""

import hashlib

from sqlalchemy import select

from auto_app.core.db import tables as T
from tests.imported_jobs import ingest, job_id_of, make_store, record

TEXT = "Build APIs in Python."


def payloads(store):
    with store.engine.connect() as conn:
        entries = [r.payload for r in conn.execute(select(T.ingest_entries.c.payload))]
        changes = [r.payload for r in conn.execute(select(T.ingest_changes.c.payload))]
    return entries, changes


def test_descriptions_are_stored_on_the_posting_and_fingerprinted_in_import_records(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer", description=TEXT)], source="board-greenhouse-acme")
    entries, changes = payloads(store)
    for payload in entries + changes:
        assert TEXT not in str(payload)
        assert payload["description_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
        assert payload["description_chars"] == len(TEXT)
    assert store.job_inputs(job_id_of(store, "Backend Engineer"))[0]["description_text"] == TEXT
    store.engine.dispose()


def test_a_changed_description_is_still_an_update(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer", description=TEXT)], source="board-greenhouse-acme")
    report = ingest(store, [record("101", "Backend Engineer", description=TEXT + " Own services.")],
                    source="board-greenhouse-acme")
    assert report["updated"] == 1
    assert store.job_inputs(job_id_of(store, "Backend Engineer"))[0]["description_text"].endswith("Own services.")
    store.engine.dispose()


def test_records_without_a_description_keep_their_previous_shape(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer")])
    entries, _ = payloads(store)
    assert entries[0]["description"] == "" and "description_sha256" not in entries[0]
    store.engine.dispose()
