# 2026-11-11 — `company_groups`: one company limit for an employer's other names

## Why

The handoff's company limit (3 submissions per company in a rolling 30 days, `docs/agent-handoff.md`) counts by
a job's `company_norm`. That name is only lowercased, stripped of punctuation and parentheticals, and stripped of
a trailing legal suffix (Inc, LLC, Corp …). An employer that postings name in different ways therefore had a
separate limit under each name. A read-only check of the live library on 2026-11-11 found, among kept, wanted and
applied jobs:

- amazon 195, amazon web services 21, amazon science 3;
- gm 47, general motors 8. "GM" is the `company` of the Workday feed added on 2026-11-07; aggregators say
  "General Motors";
- ford 71, ford motor 7, ford pro 3;
- hewlett packard enterprise 58, hpe 6;
- palantir 28, palantir technologies 15;
- the same pattern for Booz Allen, Capgemini, Micron, Intuitive Surgical, Applied Materials, JPMorgan Chase and
  others.

So up to nine applications to Amazon could pass in 30 days. Nothing had been submitted through the handoff yet
(no applied jobs, submissions or claims), so no limit had been exceeded.

## Change

- `handoff.json` accepts an optional `company_groups`: a group's name → a list of the employer's other names
  (`src/auto_app/handoff/guard.py`).
  - Every name is normalized with the same `company_basic` that produces `company_norm`, so "Amazon.com, Inc."
    and "amazon com" are one entry.
  - A file is rejected when:
    - a name is in two groups;
    - a group's name is also listed inside another group;
    - a name normalizes to nothing;
    - a group is repeated;
    - the shape is wrong.

    Limits: at most 500 groups, 50 names in a group, 200 characters in a name.
- `Guard.company_keys()` maps each listed name to its group. `HandoffService._usage` counts submissions and live
  claims per group, and `_candidates` compares a job's group with the limit. So queue, claim and packet all treat
  the names as one company.
- Nothing else reads the groups. Job identity, deduplication, the queue items' `company` field and the daily
  limit are unchanged.

## Tests

- `tests/handoff/test_guard.py`:
  - normalization, including suffixes and punctuation;
  - a group's own name inside its list is ignored;
  - ten malformed or ambiguous files are rejected;
  - the 500-group limit.
- `tests/handoff/test_service.py::test_company_groups_share_one_company_limit`:
  - with `company_limit` 1 and no group, Acme's live claim leaves an "Acme Labs, Inc." job in the queue;
  - with `{"Acme": ["Acme Labs"]}`, the claim already blocks it (`company_cooldown`), and so does the recorded
    submission;
  - claiming it then fails;
  - 31 days later it is eligible again. This also covers the window's expiry, which no test checked before.
- Handoff and jobq: 161 passed (148 before).

## Live configuration

The 38 groups installed in `runtime/config/handoff.json` on 2026-11-11 are kept conservative. They cover 47 other
names, 45 of which appear on live jobs (349 jobs).

They merge only names that are clearly one employer with one hiring channel:

- Amazon: AWS, Amazon Science, Prime Video;
- GM: General Motors;
- Ford: Ford Motor, Ford Pro, Ford Racing;
- HPE; Palantir; Booz Allen; Capgemini; Micron; Intuitive Surgical; Applied Materials (AMAT); JPMorgan Chase;
- several smaller employers.

They leave separate:

- different companies with similar names: HP Inc. and HPE, Cohere and Cohere Health, Sierra and Sierra Space,
  Epic and Epic Games, Motorola Solutions and Motorola;
- subsidiaries and joint ventures that hire on their own: Google DeepMind, the TikTok US joint venture,
  Rivian–Volkswagen, GM Financial, Citadel Securities, Accenture Federal Services, the NCR spin-offs;
- bare names that could be several companies: "aurora", "scale", "loft", "pure", "nimble", "cadence".

Add a group when the same employer shows up under a new name.
