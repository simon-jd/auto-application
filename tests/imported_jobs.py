"""Imported-job test data shared by the Linux worker's tests (screen, enrich, worker)."""

from auto_app.core.db.store import DbStore
from auto_app.ingest.models import IngestRecord, ParsedFeed
from auto_app.ingest.service import IngestService
from auto_app.settings import Paths


def make_store(tmp_path):
    return DbStore(tmp_path / "data" / "auto_app.db", Paths(home=tmp_path))


def record(external_id, title, *, sponsorship="Other", category="Software", **fields):
    values = dict(external_id=external_id, url=f"https://job-boards.greenhouse.io/acme/jobs/{external_id}",
                  title=title, company="Acme", locations=["Boston, MA"], active=True,
                  metadata={"upstream": {"sponsorship": sponsorship, "category": category}})
    values.update(fields)
    return IngestRecord(**values)


def ingest(store, records, source="simplify-newgrad"):
    return IngestService(store).ingest(source, ParsedFeed(records), kind="repository", origin="fixture.json",
                                       revision="r1", complete_snapshot=True)


def job_id_of(store, title):
    return next(j["job_id"] for j in store.jobs() if j["title"] == title)


class FakeFetcher:
    """Answers every fetch with `result` (a FetchResult, or a function of the target); records the API URLs."""

    def __init__(self, result):
        self.result, self.calls = result, []

    def fetch(self, target):
        self.calls.append(target.api_url)
        return self.result(target) if callable(self.result) else self.result


def ok(description, **fields):
    from auto_app.core.models import CommonPosting
    from auto_app.enrich.fetch import FetchResult

    return FetchResult("ok", posting=CommonPosting(source="enrichment", source_job_id="x",
                                                   title=fields.pop("title", "Software Engineer"),
                                                   description_text=description, **fields))
