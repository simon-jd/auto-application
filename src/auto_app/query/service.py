"""Queries without DbStore, migrations, secrets, or optional runtime imports.

Pagination bounds new insertions by the first page's maximum job ID. Existing rows
are read afresh on each call: the cursor is not a revision or a frozen snapshot.
"""

import base64
import binascii
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from auto_app.preferences import load_preferences, preferences_for_database


class QueryError(ValueError):
    """The database, stored JSON, or query parameters cannot be read as requested."""


STATES = ("not_evaluated", "recommended", "un_recommended", "want_to_apply", "applied", "not_applied", "archived")
STATE_SQL = """CASE WHEN j.archived_at IS NOT NULL THEN 'archived'
    WHEN j.decision IS NOT NULL AND j.decision != '' THEN j.decision
    WHEN j.step2 IN ('recommended', 'un_recommended') THEN j.step2
    ELSE 'not_evaluated' END"""
TITLE_SQL = "COALESCE(NULLIF(p.title, ''), json_extract(j.archive, '$.title'), j.title_norm)"
COMPANY_SQL = "COALESCE(NULLIF(p.company, ''), json_extract(j.archive, '$.company'), j.company_norm)"
JOIN_SQL = "FROM jobs j LEFT JOIN postings p ON p.posting_id = j.primary_posting_id"
# Raw locations matter: normalization deliberately collapses remote countries. The same evidence
# answers "names an excluded country" and "names a US place", via different SQL functions.
EVIDENCE_SQL = """(EXISTS (SELECT 1 FROM job_locations l WHERE l.job_id=j.job_id AND {loc}(l.location_norm))
    OR EXISTS (SELECT 1 FROM postings ep, json_each(ep.parsed, '$.locations') el
               WHERE ep.job_id=j.job_id AND {loc}(el.value))
    OR EXISTS (SELECT 1 FROM postings ep WHERE ep.job_id=j.job_id
               AND {code}(json_extract(ep.parsed, '$.extras.country')))
    OR EXISTS (SELECT 1 FROM json_each(j.archive, '$.locations') al WHERE {loc}(al.value))
    OR EXISTS (SELECT 1 FROM json_each(j.archive, '$.raw_locations') ar WHERE {loc}(ar.value))
    OR EXISTS (SELECT 1 FROM json_each(j.archive, '$.countries') ac WHERE {code}(ac.value)))"""
INGEST_EVIDENCE_SQL = """EXISTS (SELECT 1 FROM ingest_entries ce JOIN postings cp ON cp.posting_id=ce.posting_id
    WHERE cp.job_id=j.job_id AND {code}(json_extract(ce.payload, '$.metadata.upstream.country')))"""
EXCLUDED_SQL = EVIDENCE_SQL.format(loc="excluded_country", code="excluded_country_code")
INGEST_COUNTRY_SQL = INGEST_EVIDENCE_SQL.format(code="excluded_country_code")
US_SQL = EVIDENCE_SQL.format(loc="us_location", code="us_country_code")
INGEST_US_SQL = INGEST_EVIDENCE_SQL.format(code="us_country_code")

JOB_COLUMNS = """job_id primary_posting_id company_norm title_norm canonical_url step2 step2_at decision
    decided_at first_seen_at last_seen_at expires_at archived_at archive duplicate_of warnings notified_at""".split()
POSTING_COLUMNS = """posting_id source source_job_id source_url kind platform board state title company posted_at
    apply_url apply_kind first_seen_at last_seen_at duplicate_of match_rule match_confidence""".split()
JUDGMENT_COLUMNS = """job_id filter_version filter model status recommend fit dealbreaker reason parser parse_error
    error input_tokens output_tokens tool_calls cost_usd created_at parsed_at""".split()
REVIEW_COLUMNS = "review_id job_id decision reason filters created_at".split()
FEEDBACK_COLUMNS = "job_id filter filter_version verdict correct label origin reason created_at".split()
HEALTH_FIELDS = "cooldown_until last_block_at last_block_reason last_ok_at last_run_at".split()
INGEST_RUN_FIELDS = """status last_run_at finished_at last_ok_at error records created updated unchanged
    restored inactive missing jobs_created has_more messages_processed messages_skipped messages_gone
    messages_pending messages_deferred""".split()
MAILBOX_PROGRESS_FIELDS = "window_start window_end watermark in_progress has_more".split()
PARSED_FIELDS = """source source_job_id title company locations workplace_type employment_type seniority salary
    posted_at posted_at_precision posted_text reposted reposted_at applicants applicants_text apply_url apply_kind
    url req_id description_text description_html""".split()
INGEST_ENTRY_FIELDS = """source external_id posting_id kind origin revision content_hash status first_seen_at
    last_seen_at last_imported_at needs_reassessment""".split()
INGEST_CHANGE_FIELDS = """change_id source external_id posting_id event revision content_hash payload
    observed_at created_at""".split()
INGEST_OPTIONAL = {
    "ingest_entries": [*INGEST_ENTRY_FIELDS, "payload"],
    "ingest_changes": INGEST_CHANGE_FIELDS,
}
SCREENING_COLUMNS = {"job_id", "rules_version", "outcome", "reasons", "signals", "screened_at"}
SPONSORSHIP = ("offers", "no", "unknown", "citizenship_required")
SCREENED_OUT_SQL = ("EXISTS (SELECT 1 FROM job_screenings s WHERE s.job_id=j.job_id AND s.outcome='excluded') "
                    "AND COALESCE(j.decision, '') = ''")
SPONSORSHIP_SQL = ("EXISTS (SELECT 1 FROM job_screenings s WHERE s.job_id=j.job_id "
                   "AND json_extract(s.signals, '$.sponsorship')=?)")
PENDING_SQL = "COALESCE(json_extract(p.parsed, '$.extras.ingestion.enrichment_status'), '') = 'pending'"
COMPLETENESS_SQL = f"""SELECT p.job_id,
    MAX(CASE WHEN {PENDING_SQL} THEN 1 ELSE 0 END) AS has_pending,
    MAX(CASE WHEN json_type(p.parsed) = 'object' AND NOT ({PENDING_SQL}) THEN 1 ELSE 0 END) AS has_complete
    FROM postings p"""
SELECT_JOB = ("SELECT " + ", ".join(f"j.{c}" for c in JOB_COLUMNS)
              + f", {TITLE_SQL} AS title, {COMPANY_SQL} AS company, p.source AS primary_source, "
                "COALESCE(p.posted_at, json_extract(j.archive, '$.posted_at')) AS posted_at, "
              + STATE_SQL + " AS state " + JOIN_SQL)

# Validate only the tables/columns this interface reads, rather than requiring an
# exact migration head or the unrelated application/account/trace tables.
REQUIRED = {
    "alembic_version": ["version_num"],
    "jobs": JOB_COLUMNS,
    "postings": [*POSTING_COLUMNS, "job_id", "parsed"],
    "job_locations": ["job_id", "location_norm"],
    "judgments": JUDGMENT_COLUMNS,
    "reviews": REVIEW_COLUMNS,
    "feedback": FEEDBACK_COLUMNS,
    "source_health": ["source", "status", "data"],
    "batches": ["batch_id", "started_at", "finished_at", "interrupted"],
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _dates(row):
    """Match DbStore's ISO UTC dates for SQLite's otherwise naive text values."""
    result = dict(row)
    for key, value in result.items():
        if key.endswith("_at") and value:
            try:
                date = datetime.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise QueryError(f"Invalid stored date in {key}") from exc
            result[key] = (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).isoformat()
    return result


def _json(value, expected, field):
    if value is None:
        return None
    try:
        result = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise QueryError(f"Invalid JSON in {field}") from exc
    if result is not None and not isinstance(result, expected):
        raise QueryError(f"Invalid JSON type in {field}")
    return result


class QueryService:
    """Open and validate a database immediately; the context manager closes it."""

    def __init__(self, db_path: Path, preferences_path: Path | None = None):
        self._conn = None
        try:
            self.db_path = Path(db_path).expanduser().resolve()
            self.preferences_path = (Path(preferences_path).expanduser().resolve() if preferences_path is not None
                                     else preferences_for_database(self.db_path))
            self.preferences = load_preferences(self.preferences_path, required=preferences_path is not None)
            self._conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.create_function("excluded_country", 1,
                                       lambda location: bool(self.preferences.countries_for(location)) if location else False,
                                       deterministic=True)
            self._conn.create_function("excluded_country_code", 1,
                                       lambda code: bool(self.preferences.countries_for_code(code)), deterministic=True)
            from auto_app.screen.locations import classify_location

            self._conn.create_function("us_location", 1,
                                       lambda location: bool(location) and classify_location(location) == "us",
                                       deterministic=True)
            self._conn.create_function("us_country_code", 1,
                                       lambda code: isinstance(code, str) and code.strip().upper() in ("US", "USA"),
                                       deterministic=True)
            for table, required in REQUIRED.items():
                columns = {row["name"] for row in self._conn.execute(f'PRAGMA table_info("{table}")')}
                missing = set(required) - columns
                if missing:
                    raise QueryError(f"Incompatible database schema: {table} missing {', '.join(sorted(missing))}")
            versions = [r[0] for r in self._conn.execute("SELECT version_num FROM alembic_version")]
            if len(versions) != 1 or not versions[0]:
                raise QueryError("Incompatible database schema: expected one Alembic version")
            self.schema_version = versions[0]
            optional_columns = {
                table: {row["name"] for row in self._conn.execute(f'PRAGMA table_info("{table}")')}
                for table in INGEST_OPTIONAL
            }
            screening_columns = {row["name"] for row in self._conn.execute('PRAGMA table_info("job_screenings")')}
            event_columns = {row["name"] for row in self._conn.execute('PRAGMA table_info("application_events")')}
            self.events_available = {"event_id", "job_id", "kind", "occurred_at", "source", "note"} <= event_columns
            self.screening_available = SCREENING_COLUMNS <= screening_columns
            self.ingestion_available = all(optional_columns.values())
            excluded, in_us = ((f"({EXCLUDED_SQL} OR {INGEST_COUNTRY_SQL})", f"({US_SQL} OR {INGEST_US_SQL})")
                               if self.ingestion_available else (EXCLUDED_SQL, US_SQL))
            # Same rule as Preferences.evaluate: a job with any US place is kept.
            self._excluded_sql = (excluded if self.preferences._excludes_us()
                                  else f"({excluded} AND NOT {in_us})")
            if self.ingestion_available:
                for table, required in INGEST_OPTIONAL.items():
                    missing = set(required) - optional_columns[table]
                    if missing:
                        raise QueryError(f"Incompatible ingestion schema: {table} missing {', '.join(sorted(missing))}")
        except (OSError, sqlite3.Error, ValueError) as exc:
            if self._conn is not None:
                self._conn.close()
            if isinstance(exc, QueryError):
                raise
            raise QueryError(f"Cannot open read-only database {db_path}: {exc}") from exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._conn.close()

    @contextmanager
    def _read(self):
        """Keep one response internally consistent without holding a cursor snapshot."""
        try:
            self._conn.execute("BEGIN")
            try:
                yield
            finally:
                self._conn.rollback()
        except sqlite3.Error as exc:
            raise QueryError(f"Database query failed: {exc}") from exc

    def context(self) -> dict:
        with self._read():
            by_state = dict.fromkeys(STATES, 0)
            for row in self._conn.execute(f"SELECT {STATE_SQL} AS state, COUNT(*) AS n FROM jobs j GROUP BY state"):
                by_state[row["state"]] = row["n"]
            total = sum(by_state.values())
            sources = {row["source"]: {"source": row["source"], "posting_count": row["n"], "status": "unknown"}
                       for row in self._conn.execute("SELECT source, COUNT(*) AS n FROM postings GROUP BY source")}
            for row in self._conn.execute("SELECT source, status, data FROM source_health ORDER BY source"):
                health = _json(row["data"], dict, "source_health.data") or {}
                entry = sources.setdefault(row["source"], {"source": row["source"], "posting_count": 0})
                entry.update(status=row["status"], **{key: health[key] for key in HEALTH_FIELDS if key in health})
                for key, fields in (("ingest_run", INGEST_RUN_FIELDS), ("mailbox", MAILBOX_PROGRESS_FIELDS)):
                    if isinstance(health.get(key), dict):
                        entry[key] = {field: health[key][field] for field in fields if field in health[key]}
            batch = self._conn.execute("""SELECT batch_id, started_at, finished_at, interrupted FROM batches
                                          ORDER BY started_at DESC, batch_id DESC LIMIT 1""").fetchone()
            latest = _dates(batch) if batch else None
            if latest:
                latest["interrupted"] = bool(latest["interrupted"])
            result = {
                "schema_version": self.schema_version, "as_of": _now(), "database": str(self.db_path),
                "counts": {"total": total, "active": total - by_state["archived"], "archived": by_state["archived"],
                           "by_state": by_state, "postings": sum(x["posting_count"] for x in sources.values())},
                "sources": [sources[key] for key in sorted(sources)], "latest_batch": latest,
                "capabilities": {"read_only": True, "job_queries": True, "opportunity_qualification": False,
                                 "evidence": False, "tasks": False, "resume_providers": False,
                                 "ingestion": self.ingestion_available},
            }
            excluded = (self._conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE j.archived_at IS NULL AND {self._excluded_sql}").fetchone()[0]
                        if self.preferences.exclude_countries else 0)
            result["preferences"] = {"path": str(self.preferences_path),
                                     "configured": self.preferences_path.exists(),
                                     "exclude_countries": list(self.preferences.exclude_countries),
                                     "excluded_active_jobs": excluded,
                                     "included_active_jobs": result["counts"]["active"] - excluded}
            if self.ingestion_available:
                result["ingestion"] = self._ingestion_summary()
            result["capabilities"]["rules_screening"] = self.screening_available
            if self.screening_available:
                result["screening"] = self._screening_summary(result["counts"]["active"])
            return result

    def _ingestion_summary(self):
        """Import entries include inactive/unlinked records; job counts include only active jobs."""
        by_status = dict(self._conn.execute("SELECT status, COUNT(*) FROM ingest_entries GROUP BY status"))
        pending = self._conn.execute(f"""SELECT COUNT(*) FROM (
            {COMPLETENESS_SQL} JOIN jobs j ON j.job_id=p.job_id
            WHERE j.archived_at IS NULL
              AND EXISTS (SELECT 1 FROM ingest_entries e JOIN postings ip ON ip.posting_id=e.posting_id
                          WHERE ip.job_id=j.job_id)
            GROUP BY p.job_id HAVING has_pending=1 AND has_complete=0)""").fetchone()[0]
        reassessment = self._conn.execute("""SELECT COUNT(DISTINCT p.job_id) FROM ingest_entries e
            JOIN postings p ON p.posting_id=e.posting_id JOIN jobs j ON j.job_id=p.job_id
            WHERE e.needs_reassessment=1 AND j.archived_at IS NULL""").fetchone()[0]
        return {"entries": sum(by_status.values()), "by_status": by_status,
                "pending_jobs": pending, "needs_reassessment_jobs": reassessment}

    def _screening_summary(self, active):
        """Current verdicts of active jobs; unscreened jobs stay visible."""
        live = "FROM job_screenings s JOIN jobs j ON j.job_id=s.job_id WHERE j.archived_at IS NULL"
        counts = dict(self._conn.execute(f"SELECT s.outcome, COUNT(*) {live} GROUP BY s.outcome"))
        reasons = Counter()
        for (value,) in self._conn.execute(f"SELECT s.reasons {live} AND s.outcome='excluded'"):
            reasons.update(r.split(":", 1)[0] for r in _json(value, list, "job_screenings.reasons") or [])
        sponsorship = dict(self._conn.execute(
            f"SELECT COALESCE(json_extract(s.signals, '$.sponsorship'), 'unknown'), COUNT(*) "
            f"{live} AND s.outcome='kept' GROUP BY 1"))
        kept, excluded = counts.get("kept", 0), counts.get("excluded", 0)
        return {"kept": kept, "excluded": excluded, "unscreened": max(0, active - kept - excluded),
                "excluded_by_reason": dict(reasons.most_common()), "kept_by_sponsorship": sponsorship}

    def _screening_projection(self, job_ids):
        placeholders = ",".join("?" for _ in job_ids)
        result = {}
        for row in self._conn.execute(f"""SELECT job_id, rules_version, outcome, reasons, signals, screened_at
                                          FROM job_screenings WHERE job_id IN ({placeholders})""", job_ids):
            entry = _dates(row)
            entry["reasons"] = _json(entry["reasons"], list, "job_screenings.reasons") or []
            entry["signals"] = _json(entry["signals"], dict, "job_screenings.signals") or {}
            result[entry.pop("job_id")] = entry
        return result

    def jobs(self, query=None, state=None, company=None, source=None, archived=False, limit=50, cursor=None,
             include_excluded=False, sponsorship=None) -> dict:
        """Return jobs by descending ID. Explicit archived state overrides the archive default."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise QueryError("limit must be an integer between 1 and 200")
        if archived is not None and type(archived) is not bool:
            raise QueryError("archived must be true, false, or null")
        if type(include_excluded) is not bool:
            raise QueryError("include_excluded must be true or false")
        if state is not None and state not in STATES:
            raise QueryError(f"state must be one of: {', '.join(STATES)}")
        if sponsorship is not None and sponsorship not in SPONSORSHIP:
            raise QueryError(f"sponsorship must be one of: {', '.join(SPONSORSHIP)}")
        if sponsorship is not None and not self.screening_available:
            raise QueryError("sponsorship requires a database with screening results")
        for name, value in (("query", query), ("company", company), ("source", source)):
            if value is not None and not isinstance(value, str):
                raise QueryError(f"{name} must be a string")
        if state == "archived":
            archived = True
        filters = {"query": query, "state": state, "company": company, "source": source, "archived": archived,
                   "include_excluded": include_excluded, "sponsorship": sponsorship, "exclude_countries": list(self.preferences.exclude_countries)}
        saved = self._cursor(cursor, filters) if cursor is not None else None
        with self._read():
            max_id = saved["max_id"] if saved else self._conn.execute("SELECT COALESCE(MAX(job_id), 0) FROM jobs").fetchone()[0]
            conditions, args = ["j.job_id <= ?"], [max_id]
            if self.preferences.exclude_countries and not include_excluded:
                conditions.append(f"NOT {self._excluded_sql}")
            if self.screening_available and not include_excluded:
                conditions.append(f"NOT ({SCREENED_OUT_SQL})")
            if sponsorship is not None:
                conditions.append(SPONSORSHIP_SQL)
                args.append(sponsorship)
            if archived is not None:
                conditions.append("j.archived_at IS " + ("NOT NULL" if archived else "NULL"))
            if state is not None:
                conditions.append(f"({STATE_SQL}) = ?")
                args.append(state)
            if company:
                conditions.append(f"instr(lower(COALESCE({COMPANY_SQL}, '')), lower(?)) > 0")
                args.append(company)
            if query:
                conditions.append(f"instr(lower(COALESCE({TITLE_SQL}, '') || ' ' || COALESCE({COMPANY_SQL}, '')), lower(?)) > 0")
                args.append(query)
            if source:
                conditions.append("""(EXISTS (SELECT 1 FROM postings s WHERE s.job_id=j.job_id AND s.source=?)
                    OR EXISTS (SELECT 1 FROM json_each(j.archive, '$.postings') a
                               WHERE json_extract(a.value, '$.source')=?))""")
                args.extend([source, source])
            where = " WHERE " + " AND ".join(conditions)
            total = self._conn.execute("SELECT COUNT(*) " + JOIN_SQL + where, args).fetchone()[0]
            if saved:
                where += " AND j.job_id < ?"
                args.append(saved["last_id"])
            rows = self._conn.execute(SELECT_JOB + where + " ORDER BY j.job_id DESC LIMIT ?", [*args, limit + 1]).fetchall()
            items = self._project(rows[:limit])
            next_cursor = None
            if len(rows) > limit:
                payload = {"v": 1, "max_id": max_id, "last_id": items[-1]["job_id"], "filters": filters}
                next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
            return {"items": items, "total": total, "returned": len(items), "next_cursor": next_cursor,
                    "as_of": _now(), "snapshot_max_id": max_id}

    @staticmethod
    def _cursor(cursor, filters):
        try:
            if not isinstance(cursor, str) or len(cursor) > 16384:
                raise ValueError
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (not isinstance(value, dict) or value.get("v") != 1 or value.get("filters") != filters
                    or type(value.get("max_id")) is not int or type(value.get("last_id")) is not int
                    or not 0 < value["last_id"] <= value["max_id"] <= 9223372036854775807):
                raise ValueError
            return value
        except (ValueError, TypeError, binascii.Error, UnicodeError) as exc:
            raise QueryError("Invalid cursor or cursor does not match the query filters") from exc

    def _project(self, rows):
        if not rows:
            return []
        ids = [row["job_id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        locations, raw_locations, countries, sources, counts = {}, {}, {}, {}, {}
        for row in self._conn.execute(f"SELECT job_id, location_norm FROM job_locations WHERE job_id IN ({placeholders}) ORDER BY location_norm", ids):
            locations.setdefault(row["job_id"], []).append(row["location_norm"])
        for row in self._conn.execute(f"SELECT job_id, json_extract(parsed, '$.locations') AS locations, json_extract(parsed, '$.extras.country') AS country FROM postings WHERE job_id IN ({placeholders}) ORDER BY posting_id", ids):
            raw_locations.setdefault(row["job_id"], []).extend(_json(row["locations"], list, "postings.parsed.locations") or [])
            countries.setdefault(row["job_id"], []).append(row["country"])
        if self.ingestion_available and self.preferences.exclude_countries:
            for row in self._conn.execute(f"""SELECT p.job_id, json_extract(e.payload, '$.metadata.upstream.country') AS country
                    FROM ingest_entries e JOIN postings p ON p.posting_id=e.posting_id
                    WHERE p.job_id IN ({placeholders})""", ids):
                countries.setdefault(row["job_id"], []).append(row["country"])
        for row in self._conn.execute(f"SELECT job_id, source, COUNT(*) AS n FROM postings WHERE job_id IN ({placeholders}) GROUP BY job_id, source ORDER BY source", ids):
            sources.setdefault(row["job_id"], []).append(row["source"])
            counts[row["job_id"]] = counts.get(row["job_id"], 0) + row["n"]
        ingestion = self._ingestion_projection(ids) if self.ingestion_available else {}
        screenings = self._screening_projection(ids) if self.screening_available else {}
        result = []
        for row in rows:
            item = _dates(row)
            archive = _json(item["archive"], dict, "jobs.archive")
            item["archive"] = archive
            item["warnings"] = _json(item["warnings"], list, "jobs.warnings") or []
            old = (archive or {}).get("postings") or []
            if not isinstance(old, list) or any(not isinstance(p, dict) or not isinstance(p.get("source"), str) for p in old):
                raise QueryError("Invalid JSON postings in jobs.archive")
            jid = item["job_id"]
            item.update(locations=locations.get(jid) or (archive or {}).get("locations") or [],
                        sources=sources.get(jid) or sorted({p["source"] for p in old}),
                        postings=len(old) if archive else counts.get(jid, 0))
            item["canonical_url"] = item["canonical_url"] or (archive or {}).get("apply_url")
            item["screening"] = self.preferences.evaluate([*raw_locations.get(jid, []), *item["locations"],
                                                           *((archive or {}).get("locations") or []),
                                                           *((archive or {}).get("raw_locations") or [])],
                                                          countries=[*countries.get(jid, []),
                                                                     *((archive or {}).get("countries") or [])])
            rule = screenings.get(jid)
            if rule is not None:
                country = item["screening"]
                item["screening"] = {**country, "excluded": country["excluded"] or rule["outcome"] == "excluded",
                                     "reasons": [*country["reasons"], *rule["reasons"]], "signals": rule["signals"],
                                     "rules_version": rule["rules_version"], "screened_at": rule["screened_at"]}
            if self.ingestion_available:
                item["ingestion"] = ingestion[jid]
            result.append(item)
        return result

    def _ingestion_projection(self, job_ids, *, details=False):
        """Batch only this result's jobs; list queries never fetch source payloads or history."""
        placeholders = ",".join("?" for _ in job_ids)
        result = {jid: {"entries": [], "pending_enrichment": False, "needs_reassessment": False}
                  for jid in job_ids}
        fields = [*INGEST_ENTRY_FIELDS, "payload"] if details else INGEST_ENTRY_FIELDS
        columns = ", ".join(f"e.{field}" for field in fields)
        metadata = (", json_extract(p.parsed, '$.extras.ingestion') AS parsed_ingestion" if details else "")
        entry_rows = self._conn.execute(f"""SELECT p.job_id, {columns}{metadata}
            FROM ingest_entries e JOIN postings p ON p.posting_id=e.posting_id
            WHERE p.job_id IN ({placeholders}) ORDER BY e.source, e.external_id""", job_ids)
        entries = {}
        for row in entry_rows:
            entry = _dates(row)
            jid = entry.pop("job_id")
            entry["needs_reassessment"] = bool(entry["needs_reassessment"])
            if details:
                entry["payload"] = _json(entry["payload"], dict, "ingest_entries.payload")
                entry["parsed_ingestion"] = _json(entry["parsed_ingestion"], dict, "postings.parsed.extras.ingestion")
                entry["changes"] = []
            result[jid]["entries"].append(entry)
            result[jid]["needs_reassessment"] |= entry["needs_reassessment"]
            entries[(entry["source"], entry["external_id"])] = entry
        # Consider complete linked evidence from every source, including sources
        # that predate imports, before labelling a job as awaiting enrichment.
        for row in self._conn.execute(f"{COMPLETENESS_SQL} WHERE p.job_id IN ({placeholders}) GROUP BY p.job_id", job_ids):
            result[row["job_id"]]["pending_enrichment"] = bool(row["has_pending"] and not row["has_complete"])
        if details and entries:
            columns = ", ".join(f"c.{field}" for field in INGEST_CHANGE_FIELDS)
            for row in self._conn.execute(f"""SELECT {columns} FROM ingest_changes c
                JOIN ingest_entries e ON e.source=c.source AND e.external_id=c.external_id
                JOIN postings p ON p.posting_id=e.posting_id
                WHERE p.job_id IN ({placeholders}) ORDER BY c.change_id""", job_ids):
                change = _dates(row)
                change["payload"] = _json(change["payload"], dict, "ingest_changes.payload")
                entries[(change["source"], change["external_id"])]["changes"].append(change)
        return result

    def job(self, job_id: int) -> dict | None:
        """Read one ID directly, including saved archive summaries and user feedback."""
        if type(job_id) is not int or not 0 < job_id <= 9223372036854775807:
            raise QueryError("job_id must be a positive integer")
        with self._read():
            row = self._conn.execute(SELECT_JOB + " WHERE j.job_id=?", (job_id,)).fetchone()
            if row is None:
                return None
            item = self._project([row])[0]
            parsed_row = self._conn.execute("SELECT parsed FROM postings WHERE posting_id=?", (item["primary_posting_id"],)).fetchone()
            parsed = _json(parsed_row[0], dict, "postings.parsed") if parsed_row else None
            item["parsed"] = {k: parsed[k] for k in PARSED_FIELDS if k in parsed} if parsed is not None else None
            postings = [_dates(p) for p in self._conn.execute(
                "SELECT " + ", ".join(POSTING_COLUMNS) + " FROM postings WHERE job_id=? ORDER BY posting_id", (job_id,))]
            for posting in postings:
                posting["source_job_id"] = posting["source_job_id"].split("#archived", 1)[0]
            item["posting_details"] = postings or (item["archive"] or {}).get("postings") or []
            item["events"] = ([_dates(r) for r in self._conn.execute(
                "SELECT event_id, kind, occurred_at, source, note FROM application_events "
                "WHERE job_id=? ORDER BY occurred_at, event_id", (job_id,))] if self.events_available else [])
            if self.ingestion_available:
                item["ingestion"] = self._ingestion_projection([job_id], details=True)[job_id]
            for name, columns, order in (("judgments", JUDGMENT_COLUMNS, "filter, created_at, filter_version"),
                                         ("reviews", REVIEW_COLUMNS, "review_id"),
                                         ("feedback", FEEDBACK_COLUMNS, "filter")):
                item[name] = [_dates(r) for r in self._conn.execute(
                    f"SELECT {', '.join(columns)} FROM {name} WHERE job_id=? ORDER BY {order}", (job_id,))]
            for judgment in item["judgments"]:
                for key in ("recommend", "fit", "dealbreaker"):
                    judgment[key] = bool(judgment[key]) if judgment[key] is not None else None
            for review in item["reviews"]:
                review["filters"] = _json(review["filters"], list, "reviews.filters")
            for feedback in item["feedback"]:
                for key in ("verdict", "correct", "label"):
                    feedback[key] = bool(feedback[key]) if feedback[key] is not None else None
            return item
