"""Whether a job's postings are still open, from saved feed evidence. Shared by the apply queue (`source_inactive`,
`link_gone`) and the Discord digest, which skips jobs the queue would refuse (2026-11-19)."""

from sqlalchemy import select

from auto_app.core.db import tables as T


def feed_states(conn, job_ids) -> dict[int, list[str]]:
    """Each job's evidence: its feed entries' statuses, and 'active' for a linked native browser/API posting.

    `job_ids` is a list or a select of job IDs.
    """
    p = T.postings
    states = {}
    rows = conn.execute(select(p.c.job_id, p.c.kind, p.c.state, p.c.duplicate_of, p.c.parsed,
                               T.ingest_entries.c.status.label('entry_status'))
                        .select_from(p.outerjoin(T.ingest_entries, T.ingest_entries.c.posting_id == p.c.posting_id))
                        .where(p.c.job_id.in_(job_ids))).mappings()
    for posting in rows:
        state = posting['entry_status']
        if state is None:
            # Native browser/API postings have no ingest_entries row. A linked native record is
            # independent positive evidence; a feed's disappearance does not close that posting.
            imported = 'ingestion' in ((posting['parsed'] or {}).get('extras') or {})
            if (posting['kind'] not in ('browser', 'api') or posting['state'] != 'linked'
                    or posting['duplicate_of'] is not None or imported):
                continue
            state = 'active'
        states.setdefault(posting['job_id'], []).append(state)
    return states


def closed(states: list[str] | None) -> bool:
    """Every piece of evidence says the posting is gone (no evidence at all is not closed)."""
    return bool(states) and all(s in ('inactive', 'missing') for s in states)


def gone_links(conn) -> set[str]:
    """Apply links whose employer record answered "gone"."""
    return set(conn.execute(select(T.enrichments.c.url).where(T.enrichments.c.status == 'gone')).scalars())
