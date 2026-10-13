"""Step 1b dedup (docs/step1-collect.md §7): Level 1b (same source, different ID) and Level 2
(across sources) over normalized postings, grouped with vetoes checked at every merge.

This module computes the grouping; `auto-app process dedup --preview` reports it without writing anything.
Writing jobs (`jobs`, `job_keys`, `job_locations`) comes with the database (C8c).
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from auto_app.process.normalize import clean_description, pending_enrichment
from auto_app.process.similarity import containment, containment_estimate, shingles

# Thresholds (§7.3); tuned during calibration (§7.6)
CONTAINMENT_MATCH = 0.95
CONTAINMENT_VETO = 0.5
MIN_SHINGLES = 20  # fewer = "no real description"
MIN_HASH_CHARS = 300  # identical-hash rule only for substantial descriptions
PROBABLE_WINDOW = timedelta(days=14)


@dataclass
class Item:
    pid: str  # "<source>/<source_job_id>"
    source: str
    source_job_id: str
    is_company_source: bool
    title: str
    company: str
    title_norm: str
    employer_key: str | None
    req_id: str | None
    desc_hash: str | None
    desc_chars: int
    shingle_set: set[str]
    minhash: list[int]
    shingle_count: int
    employment_type: str
    locations: list[str]
    apply_kind: str
    posted_at: datetime | None
    url: str | None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    pending_enrichment: bool = False
    title_observed: bool = True
    source_key: str | None = None

    @property
    def has_description(self) -> bool:
        return self.shingle_count >= MIN_SHINGLES


@dataclass
class Link:
    a: str
    b: str
    level: str  # "1b" | "2"
    rule: str
    confidence: float
    detail: str = ""


@dataclass
class Group:
    members: list[str]  # canonical postings (Level 2 members)
    duplicates: dict[str, tuple[str, str]] = field(default_factory=dict)  # pid → (canonical pid, rule) from Level 1b
    links: list[Link] = field(default_factory=list)
    primary: str | None = None
    locations: list[str] = field(default_factory=list)

    @property
    def all_postings(self) -> list[str]:
        return [*self.members, *self.duplicates]


@dataclass
class DedupResult:
    items: dict[str, Item]
    groups: list[Group]
    near_misses: list[tuple[str, str, str]]  # (a, b, why not merged)

    @property
    def merged(self) -> list[Group]:
        return [g for g in self.groups if len(g.all_postings) > 1]


# ---- loading ---------------------------------------------------------------------------------

def load_items(store, registry, *, source=None, job_ids=None, include_linked=False) -> dict[str, Item]:
    """Load normalized evidence in one database read, optionally scoped to affected jobs.

    ``source`` plus ``include_linked`` selects new source rows and all existing
    candidates. File stores retain their original processing interface.
    """
    companies = registry.companies()
    items: dict[str, Item] = {}
    for posting in _item_rows(store, source=source, job_ids=job_ids, include_linked=include_linked):
        n, p = posting.get("normalized"), posting.get("parsed")
        if not n or not p:
            continue
        source = posting["source"]
        pid = f"{source}/{posting['source_job_id']}"
        posted = p.get("posted_at")
        ingestion = (p.get("extras") or {}).get("ingestion") or {}
        items[pid] = Item(
            pid=pid,
            source=source,
            source_job_id=p["source_job_id"],
            is_company_source=source in companies,
            title=p["title"],
            company=n["company_norm"],
            title_norm=n["title_norm"],
            employer_key=n.get("employer_key"),
            req_id=n.get("req_id"),
            desc_hash=n.get("desc_hash"),
            desc_chars=n.get("desc_chars") or 0,
            shingle_set=shingles(clean_description(p.get("description_text") or "")),
            minhash=n.get("minhash") or [],
            shingle_count=n.get("shingle_count") or 0,
            employment_type=n.get("employment_type") or "unknown",
            locations=n.get("location_norm") or [],
            apply_kind=p.get("apply_kind") or "unknown",
            posted_at=datetime.fromisoformat(posted) if posted else None,
            url=p.get("apply_url") or p.get("url"),
            first_seen_at=posting.get("first_seen_at"),
            last_seen_at=posting.get("last_seen_at"),
            pending_enrichment=pending_enrichment(p),
            source_key=n.get("source_key"),
            title_observed=ingestion.get("title_observed", True) if isinstance(ingestion, dict) else True,
        )
    return items



def _item_rows(store, *, source=None, job_ids=None, include_linked=False):
    if getattr(store, "kind", None) == "db":
        from sqlalchemy import or_, select
        from auto_app.core.db import tables as T
        from auto_app.core.db.store import iso

        c = T.postings.c
        query = select(c.source, c.source_job_id, c.parsed, c.normalized,
                       c.first_seen_at, c.last_seen_at)
        filters = []
        if source is not None:
            filters.append(c.source == source)
        if job_ids is not None:
            filters.append(c.job_id.in_(job_ids))
        if include_linked:
            filters.append(c.job_id.is_not(None))
        if filters:
            query = query.where(or_(*filters))
        query = query.order_by(c.source, c.source_job_id)
        with store.engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        for row in rows:
            yield {**row, "first_seen_at": iso(row["first_seen_at"]),
                   "last_seen_at": iso(row["last_seen_at"])}
        return
    if job_ids is not None or include_linked:
        raise ValueError("scoped job evidence requires a database store")
    for ref in store.iter_postings(source) if source else store.iter_postings():
        yield {**(store.get_posting(ref) or {}), "source": ref.source, "source_job_id": ref.job_id,
               "normalized": store.get_normalized(ref), "parsed": store.get_parsed(ref)}


# ---- pair rules --------------------------------------------------------------------------------

_W = re.compile(r"[a-z0-9]+")


def title_similarity(a: str, b: str) -> int:
    """Shared words over the shorter title's words, 0–100 (order-insensitive)."""
    wa, wb = set(_W.findall(a)), set(_W.findall(b))
    if not wa or not wb:
        return 0
    return round(100 * len(wa & wb) / min(len(wa), len(wb)))


def desc_containment(a: Item, b: Item) -> float:
    if a.shingle_set and b.shingle_set:
        return containment(a.shingle_set, b.shingle_set)
    return containment_estimate(a.minhash, b.minhash, a.shingle_count, b.shingle_count)


def same_requisition(a: str | None, b: str | None) -> bool:
    """Text copies can omit a numeric requisition's JR/R/REQ prefix."""
    if not a or not b:
        return False
    if a == b:
        return True
    return bool((a.isdigit() and re.fullmatch(r"(?:JR|R|REQ)[_-]?" + re.escape(a), b))
                or (b.isdigit() and re.fullmatch(r"(?:JR|R|REQ)[_-]?" + re.escape(b), a)))


def veto(a: Item, b: Item) -> str | None:
    """Why a and b must not be the same job (None = no objection). ATS identity overrides."""
    if a.source_key and a.source_key == b.source_key:
        return None
    if a.employer_key and a.employer_key == b.employer_key:
        # Generic job URLs can be reused for distinct requisitions.
        if not a.employer_key.startswith("url:"):
            return None
        if a.company and b.company and a.company != b.company:
            return f"companies differ ({a.company!r} ≠ {b.company!r})"
        if a.req_id and b.req_id and not same_requisition(a.req_id, b.req_id):
            return f"requisition IDs differ ({a.req_id} ≠ {b.req_id})"
    if a.title_observed and b.title_observed and a.title_norm and b.title_norm and a.title_norm != b.title_norm:
        # the poster titled them differently, so they treat them as different jobs (e.g. "… Grad 2026" vs "… 2027")
        return f"titles differ ({a.title_norm!r} ≠ {b.title_norm!r})"
    if "unknown" not in (a.employment_type, b.employment_type) and a.employment_type != b.employment_type:
        return f"employment type {a.employment_type} ≠ {b.employment_type}"
    if a.company and a.company == b.company and a.req_id and b.req_id and not same_requisition(a.req_id, b.req_id):
        return f"requisition IDs differ ({a.req_id} ≠ {b.req_id})"
    if a.employer_key and b.employer_key and not same_requisition(a.req_id, b.req_id):
        left, right = a.employer_key.split(":", 2), b.employer_key.split(":", 2)
        if (len(left) == len(right) == 3 and left[0] in {"workday", "ashby", "greenhouse"}
                and left[:2] == right[:2] and left[2] != right[2]):
            # Workday may publish one requisition under per-location suffixes.
            # Lever also uses independent UUIDs for location copies, so a UUID
            # difference alone never overrides the content checks below.
            same_workday_req = (left[0] == "workday"
                                and re.sub(r"(?<=\d)-\d+$", "", left[2]) == re.sub(r"(?<=\d)-\d+$", "", right[2]))
            if not same_workday_req:
                return f"employer IDs differ ({left[2]} ≠ {right[2]})"
    if a.has_description and b.has_description:
        c = desc_containment(a, b)
        if c < CONTAINMENT_VETO:
            return f"descriptions differ (overlap {c:.2f})"
    return None


def match(a: Item, b: Item) -> tuple[str, float, str] | None:
    """The first Level 2 rule that holds (rule, confidence, detail), else None."""
    if veto(a, b):
        return None
    if a.employer_key and a.employer_key == b.employer_key:
        return "employer_key", 1.0, a.employer_key
    if a.source_key and a.source_key == b.source_key:
        return "source_key", 1.0, a.source_key
    # Missing evidence cannot establish a match. It is not a conflict against
    # another member of a job already reached through an exact identity match.
    if a.pending_enrichment or b.pending_enrichment:
        return None
    same_company = bool(a.company) and a.company == b.company
    if same_company and same_requisition(a.req_id, b.req_id):
        return "req_id", 0.95, a.req_id
    if same_company and a.has_description and b.has_description:  # titles are equal here (veto above)
        c = desc_containment(a, b)
        if c >= CONTAINMENT_MATCH:
            return "description", 0.9, f"overlap {c:.2f}, same title"
    if a.desc_hash and a.desc_hash == b.desc_hash and min(a.desc_chars, b.desc_chars) >= MIN_HASH_CHARS:
        return "desc_hash", 0.9, "identical description"
    if same_company and a.title_norm == b.title_norm and not (a.has_description and b.has_description):
        if a.posted_at and b.posted_at and abs(a.posted_at - b.posted_at) <= PROBABLE_WINDOW:
            return "probable", 0.6, "same company + title, a description is missing"
    return None


def near_miss(a: Item, b: Item) -> str | None:
    """A pair that looks related but wasn't matched — shown for calibration."""
    if not (a.company and a.company == b.company):
        return None
    if a.has_description and b.has_description:
        c, t = desc_containment(a, b), title_similarity(a.title_norm, b.title_norm)
        if c >= CONTAINMENT_MATCH and a.title_norm != b.title_norm:
            return f"descriptions match (overlap {c:.2f}) but titles differ (similarity {t}) — different jobs"
        if CONTAINMENT_VETO <= c < CONTAINMENT_MATCH:
            return f"description overlap {c:.2f} (between {CONTAINMENT_VETO} and {CONTAINMENT_MATCH}), title {t}"
    if a.title_norm == b.title_norm:
        v = veto(a, b)
        if v:
            return f"same title, but {v}"
    return None


# ---- grouping ------------------------------------------------------------------------------------

class _Groups:
    """Union-find over postings that refuses a merge if any cross pair is vetoed."""

    def __init__(self, items: dict[str, Item], pids: list[str], duplicates: dict[str, tuple[str, str]]):
        self.items = items
        self.parent = {p: p for p in pids}
        self.members = {p: [p] for p in pids}
        # Level 1b duplicates travel with their canonical posting, so they take part in veto checks too
        self.with_dups: dict[str, list[str]] = {p: [p] for p in pids}
        for dup, (canon, _) in duplicates.items():
            self.with_dups[canon].append(dup)

    def find(self, p: str) -> str:
        while self.parent[p] != p:
            self.parent[p] = self.parent[self.parent[p]]
            p = self.parent[p]
        return p

    def try_union(self, a: str, b: str) -> str | None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return None
        side_a = [x for m in self.members[ra] for x in self.with_dups[m]]
        side_b = [y for m in self.members[rb] for y in self.with_dups[m]]
        for x in side_a:
            for y in side_b:
                v = veto(self.items[x], self.items[y])
                if v:
                    return f"blocked: {x} and {y} — {v}"
        self.parent[rb] = ra
        self.members[ra] += self.members.pop(rb)
        return None


def l1b_keys(it: Item) -> list[tuple]:
    """Level 1b identity keys: exact, and always within the posting's own source."""
    keys = []
    if it.employer_key:
        keys.append(("employer_key", it.source, it.employer_key))
    if it.source_key:
        keys.append(("source_key", it.source, it.source_key))
    if not it.pending_enrichment and it.desc_hash and it.desc_chars >= MIN_HASH_CHARS:
        keys.append(("desc_hash", it.source, it.company, it.title_norm, it.desc_hash))
    return keys


def employment_conflict(a: Item, b: Item) -> bool:
    return "unknown" not in (a.employment_type, b.employment_type) and a.employment_type != b.employment_type


def posting_order(it: Item):
    """Earliest first — the earliest posting of a listing is canonical."""
    return (it.posted_at.timestamp() if it.posted_at else float("inf"), it.pid)


def _level1b(items: dict[str, Item]) -> dict[str, tuple[str, str]]:
    """Same source, different id → {duplicate pid: (canonical pid, rule)}. Strict: exact hash or employer key."""
    dup: dict[str, tuple[str, str]] = {}
    by_source: dict[str, list[Item]] = defaultdict(list)
    for it in items.values():
        by_source[it.source].append(it)
    for its in by_source.values():
        its.sort(key=lambda i: (i.posted_at.timestamp() if i.posted_at else float("inf"), i.pid))  # earliest = canonical
        seen: dict[tuple, str] = {}
        for it in its:
            keys = []
            if it.employer_key:
                keys.append(("employer_key", it.employer_key))
            if it.source_key:
                keys.append(("source_key", it.source_key))
            if not it.pending_enrichment and it.desc_hash and it.desc_chars >= MIN_HASH_CHARS:
                keys.append(("desc_hash", it.company, it.title_norm, it.desc_hash))
            hit = next((k for k in keys if k in seen), None)
            canon = items[seen[hit]] if hit else None
            conflict = canon is not None and (employment_conflict(it, canon) or veto(it, canon))
            if hit and not conflict:
                dup[it.pid] = (canon.pid, hit[0])
            else:
                for k in keys:
                    seen.setdefault(k, it.pid)
    return dup


def _candidates(canon: list[Item]) -> set[tuple[str, str]]:
    """Pairs worth comparing: same employer key, company, req id, or description hash."""
    index: dict[tuple, list[str]] = defaultdict(list)
    for it in canon:
        if it.employer_key:
            index[("k", it.employer_key)].append(it.pid)
        if it.source_key:
            index[("s", it.source_key)].append(it.pid)
        if it.company:
            index[("c", it.company)].append(it.pid)
        if it.desc_hash and it.desc_chars >= MIN_HASH_CHARS:
            index[("h", it.desc_hash)].append(it.pid)
    pairs = set()
    for pids in index.values():
        for i, a in enumerate(pids):
            for b in pids[i + 1:]:
                pairs.add(tuple(sorted((a, b))))
    return pairs


def _primary(items: dict[str, Item], pids: list[str]) -> str:
    """Prefer complete evidence and observed titles, then source authority and description length."""
    rank = {"external": 1, "onsite": 2, "unknown": 2, "easy_apply": 3}
    return min(pids, key=lambda p: (items[p].pending_enrichment, not items[p].title_observed,
                                    0 if items[p].is_company_source else rank.get(items[p].apply_kind, 2),
                                    -items[p].desc_chars, p))


def dedup(items: dict[str, Item]) -> DedupResult:
    duplicates = _level1b(items)
    canon = [it for pid, it in items.items() if pid not in duplicates]
    links: list[Link] = [Link(d, c, "1b", rule, 1.0 if rule in {"employer_key", "source_key"} else 0.95)
                         for d, (c, rule) in duplicates.items()]
    near: list[tuple[str, str, str]] = []

    level2: list[Link] = []
    for a_pid, b_pid in sorted(_candidates(canon)):
        a, b = items[a_pid], items[b_pid]
        m = match(a, b)
        if m:
            level2.append(Link(a_pid, b_pid, "2", *m))
        else:
            why = near_miss(a, b)
            if why:
                near.append((a_pid, b_pid, why))

    uf = _Groups(items, [it.pid for it in canon], duplicates)
    kept: list[Link] = []
    for link in sorted(level2, key=lambda l: -l.confidence):  # strongest links first
        blocked = uf.try_union(link.a, link.b)
        if blocked:
            near.append((link.a, link.b, f"{link.rule} match {blocked}"))
        else:
            kept.append(link)

    groups: dict[str, Group] = {}
    for it in canon:
        root = uf.find(it.pid)
        groups.setdefault(root, Group(members=list(uf.members[root])))
    by_member = {m: g for g in groups.values() for m in g.members}
    for d, (c, rule) in duplicates.items():
        by_member[c].duplicates[d] = (c, rule)
    for link in kept + links:
        target = by_member.get(link.a) or by_member[duplicates[link.a][0]]
        target.links.append(link)
    for g in groups.values():
        g.primary = _primary(items, g.members)
        g.locations = sorted({loc for p in g.all_postings for loc in items[p].locations})
    ordered = sorted(groups.values(), key=lambda g: (-len(g.all_postings), g.primary))
    return DedupResult(items, ordered, near)


# ---- report ----------------------------------------------------------------------------------------

def format_preview(result: DedupResult, show_near: bool = True, limit: int = 50) -> str:
    items = result.items
    n_dup = sum(len(g.duplicates) for g in result.groups)
    out = [
        f"{len(items)} postings → {len(items) - n_dup} after Level 1b (same source) → {len(result.groups)} jobs after Level 2",
        f"{len(result.merged)} jobs have more than one posting; {len(result.near_misses)} near misses",
        "",
    ]
    for i, g in enumerate(result.merged[:limit], start=1):
        head = items[g.primary]
        out.append(f"job {i}: {head.title}  —  {head.company}  ({'; '.join(g.locations) or 'no location'})")
        for p in g.all_postings:
            it = items[p]
            tag = "★" if p == g.primary else " "
            dup = g.duplicates.get(p)
            how = f"duplicate of {dup[0]} (1b {dup[1]})" if dup else ""
            out.append(f"   {tag} {p:<34} {it.apply_kind:<10} {it.title[:48]:<48} {how}")
        for link in g.links:
            if link.level == "2":
                out.append(f"       2  {link.a} ~ {link.b}: {link.rule} ({link.confidence}) {link.detail}")
        out.append("")
    if len(result.merged) > limit:
        out.append(f"… {len(result.merged) - limit} more merged jobs")
    if show_near and result.near_misses:
        out.append("near misses (not merged — check these when calibrating):")
        for a, b, why in result.near_misses[:limit]:
            out.append(f"   {a} ↔ {b}: {why}")
            out.append(f"        {items[a].title[:60]}  |  {items[b].title[:60]}")
    return "\n".join(out)


def to_json(result: DedupResult) -> dict:
    return {
        "postings": len(result.items),
        "jobs": [
            {"primary": g.primary, "postings": g.all_postings, "duplicates": {k: list(v) for k, v in g.duplicates.items()},
             "links": [vars(link) for link in g.links], "locations": g.locations,
             "title": result.items[g.primary].title, "company": result.items[g.primary].company}
            for g in result.groups
        ],
        "near_misses": [{"a": a, "b": b, "why": w} for a, b, w in result.near_misses],
    }
