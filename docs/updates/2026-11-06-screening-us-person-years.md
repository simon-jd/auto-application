# 2026-11-06 — Screening: US-person, clearance and years fixes; opt-in software-evidence rule; senior suffixes; `worker boards` no longer crashes

**Step:** 2, 4   **Status:** done locally (not deployed; rules version 8)

## Why

The 2026-11-06 embedded-boards audit (`Search job/reports/2026-11-06-embedded/`) found three deterministic-screening
defects:

1. `citizenship_required` missed the common ITAR and clearance wordings, so about 900 US-person or clearance jobs
   were kept. SpaceX, Mach Industries, Rendezvous Robotics and Revel enumerate the US-person classes ("applicant must
   be a (i) U.S. citizen or national, (ii) …"). Saronic quotes the term ("require “U.S. Person” status"). Muon writes
   "must either be (A) a U.S. person". Castelion and MatX make the offer contingent on access "without" an export
   license. Anduril puts a level between "U.S." and "clearance" ("eligible to obtain and maintain an active U.S.
   Secret security clearance"). Many federal postings state the clearance only in the title ("… with Security
   Clearance", "Cleared", "TS/SCI w/ Poly").
2. `years_required` read "1~3 years" as 3 (job 11504, Seres Auto). It also counted a degree-or-experience
   alternative as a hard requirement: "Bachelor's degree in CS … or 3+ years" (K2 Space 1248, Visa, Shield AI)
   and Cisco's "Meet one of the following:" route list (13042).
3. `auto-app worker boards` stopped on the live database with `ValueError`. `board_location` accepted only
   `[A-Za-z0-9_-]` names, but Ashby board names may contain dots or spaces (`rivianvw.tech`, `jerry.ai`,
   `spectral labs`). Enrichment has accepted these names since 2026-11-02.

## Changes (files)

- `src/auto_app/screen/rules.py` (`RULES_VERSION` 7 → 8, so every job is screened again):
  - **US person / citizenship** (`CITIZENSHIP`, `STATUS_ITEM`). These wordings now count: enumerated lists
    ("(i)", "(A)", "1)"); quoted terms; "must qualify as" / "must therefore either be"; "only for / open to … only /
    restricted to U.S. persons"; "U.S. citizen or green card holder (is) required"; "requires the successful candidate
    to be a U.S. person"; "U.S. Person(s) status is required"; access "without (obtaining / additional) export
    license(ing)"; and a list item that is only a status ("- U.S. Person status (ITAR requirement)",
    "- U.S. Citizen or Lawful Permanent Resident").
  - **Security clearance.** A named clearance (Secret, TS, SCI, DOE Q/L, security, government, federal, DoD or
    U.S. clearance) counts when the sentence also has a requirement cue: required, must, ability, eligible,
    obtain, possess, hold, maintain, active, current or willing. It also counts as a short list item
    ("- Secret clearance") or as a heading/value pair ("Security Clearance" over "Top Secret/SCI … required",
    "Minimum Clearance Required to Start: TS/SCI").
  - **Title evidence** (`TITLE_CLEARANCE`): cleared, clearance, TS/SCI, Top Secret, polygraph/poly, Active
    Secret, "- Secret -", and U.S. citizen(ship)/person in the title. "Customs clearance" and "FDA-cleared" do not count.
  - **Not requirements:**
    - Hedged sentences: may, might, could, typically, generally, if applicable / required / needed, where
      required / permitted, certain / some positions, so designated.
    - A license route: "may require an export license", GE's "contingent on ability to obtain authorization",
      Databricks' licence discretion, Analog Devices' licensing review.
    - Pay statements that list security clearances among pay factors.
    - Facility, TSA/airport and badge clearances, and HR "security clearance processing".
    - "not limited to U.S. citizens", and statements that also admit non-citizens or visa holders
      ("only U.S. citizens and lawfully authorized non-U.S. citizens", "OPT | … | US Citizen").
    - Definitions of "U.S. person", and EEO text.
    - Public Trust, which is a suitability check, not a security clearance.
    - Items in a preferred section ("Preferred qualifications", "Nice to Have", "Ways to stand out from the
      crowd", "Bonus", a heading line ending in ":" that names one of these). The section ends at the first line
      that is not a list item, so a later ITAR paragraph is read again. An item under such a heading that says
      "required" or "must" still counts.
  - `citizenship_evidence(description)` returns the sentence that triggered the exclusion. The audit uses it;
    stored signals are unchanged.
  - **Years:** `~` is a range separator (`1~3 years` = 1). A degree route in a named field ("Bachelor's degree
    in CS … or 3+ years") is an alternative only when no experience route names a degree, so "BS in CS or MS in CS
    and 3 years" stays 3. In a "one / any / either of the following" list, each item that names a credential is
    an alternative route, and the least demanding route counts. The list ends at the first item that names no
    credential, so a later "5+ years" stays hard.
- **Opt-in noise rule** (`exclude_unknown_without_software_evidence`, default `false`):
  - It applies to a job whose title names no role (`role == "unknown"`) and that has a description.
  - Such a job is excluded as `no_software_evidence` when `SOFTWARE_EVIDENCE` finds no software skill in the
    description: a language, programming, computer science, APIs, Git, Linux, cloud, ML, embedded or firmware,
    software development or engineering, full-stack / back-end / front-end, CI/CD, DevOps or databases.
  - Jobs without a description are never touched.
  - "C++" and "C#" are matched with `(?<!\w)…(?!\w)`. The proposed `\b(?:…c\+\+…c#…)\b` could not match them
    before a space or period, so a JD naming only C++ would have been excluded.
  - The live `screening.json` is not changed. The flag is to be turned on through ControlService after
    deployment.
- **Senior level suffixes**: a comma- or dash-separated title part made only of level words (senior, sr, staff,
  principal, lead, optional dots) is senior: "Software Engineer, Sr. Staff - Power and Limits Management",
  "Systems Engineer - Chipset Power, Senior Staff". "Member of Technical Staff", "- Staffing Platform",
  "- Staff Tools" and ", Lead Generation" stay kept.
- `config/screening.example.json` lists the new key, set to `false`.
- `src/auto_app/worker/boards.py`: Ashby names are checked with `ASHBY_BOARD` (the enrichment pattern) and
  percent-encoded (`spectral%20labs`). The Ashby list API is case-insensitive and accepts both forms (checked
  2026-11-06: `rivianvw.tech`, `jerry.ai`, `spectral%20labs` and `Superhuman%20Platform%20Inc` all return 200).
  A name that no list API accepts is now skipped instead of ending the listing. Greenhouse and Lever names are
  unchanged.
- Tests: `tests/screen/test_rules.py` (real employer sentences from the live database: hard, soft and
  false-positive cases, title evidence, years ranges/routes, real ≥3-year requirements that must stay hard),
  `tests/worker/test_boards.py` (dotted/spaced Ashby names, other platforms still strict, a listing with such
  boards).
- Docs: [Linux worker §2 and §6](../linux-worker.md), [project README](../../README.md).

## Live impact (read-only, 2026-11-06 ~20:20 PT)

Old and new code were run on the same facts and the same `config/screening.json`, opened with `mode=ro`, over
30,012 active jobs. The host's config had just gained `extra_non_cs_terms` from another session. This is why
902 saved verdicts were not yet current: the worker re-screens them in its next round, independent of this
change.

- **831 kept jobs become excluded**, all for `citizenship_required`, across 141 companies. The evidence is a
  clearance sentence for 472, a US-person / citizenship / export sentence for 232, and the title for 127.
  The largest companies: Anduril 224, SpaceX 133, Palantir 70, Saronic 65, Booz Allen 44, Microsoft 15
  (CTJ/Poly titles), CACI 12, KBR 9. Then Guidehouse, TSC, 9to9, Mach Industries, Peraton and Mapjects with 7
  each, and Leidos, General Dynamics, Draper, Accenture Federal and Revel with 6 each.
- **14 excluded jobs are kept again.** Six are years fixes: Visa ×3 "Bachelor's degree, OR 3+ years", Cisco
  13042, Seres 11504 and Shield AI 40990. Eight were excluded for a hedged or preferred clearance mention:
  Gallatin AI ×4 "may require …", Everfox "if needed", Shield AI 27135, Anthropic 21960 and L3Harris 41129 under
  "Preferred qualifications", and Northeastern 27744 under "Preferred Skills".
- 1,736 jobs stay excluded with different reasons. 1,706 gain `citizenship_required`. 27 lose it but stay
  excluded for another reason. 3 have a different years value.
- **No user decision is affected.** None of the changed jobs has `jobs.decision`, `jobs.step2`, an application,
  review, feedback, handoff claim, application event, resume artifact or judgment. The only active decided job
  (one `want_to_apply`) is unchanged.

A second run at ~20:38 PT used the same method on 32,699 active jobs, with the live config plus the flag
turned on:

- **The noise rule would exclude 469 kept jobs.** None has a decision, a step-2 verdict, an application, review,
  feedback, handoff claim, event, resume artifact or judgment. None is among the 586 jobs in
  `reports/2026-11-06-embedded/embedded-jobs.csv`.
- The largest companies are Relativity Space 25, OpenAI 22, ElevenLabs 16, and Shield AI, Skydio,
  Whiting-Turner and PAE 9 each.
- Examples: Five Rings "Recent Graduate - Trading Assistant", ADI "Entry-Level Applications Engineer", Cognition
  "Paid Media Marketer", ElevenLabs "Field Marketer".
- The C++/C# correction changes none of today's jobs.
- **The senior fix excludes no job that is kept today.** One job that is already excluded gains `senior_title`.
  The Qualcomm-style suffix titles are not in the live database; titles that start with "Sr. Staff" were
  already caught.

The audit's false-positive review checked all 356 distinct triggering sentences of the first pass. It found
pay statements (Guidehouse, MicroHealth, Kentro), a TSA airport badge (San Francisco), an HR duty, a section
heading, Bellevue College's "only U.S. citizens and lawfully authorized non-U.S. citizens", Arrow's "not limited
to U.S. citizens", and preferred-section mentions (NVIDIA's new-grad public-sector role, Handshake, Anthropic,
Boeing). Each one is a regression test now. The per-job list (job, company, title, change, reasons, triggering
sentence) is `Search job/reports/2026-11-06-embedded/itar-screening-impact.csv`, outside the repository.

## How to use

Nothing to configure. After deployment, the next `worker run` (or `--steps screen`) re-screens every job
because the rules version changed. `uv run auto-app-query jobs --include-excluded` shows the new reasons.
`auto-app worker boards --yaml` now lists Ashby boards such as `board-ashby-rivianvw-tech`.

## Tests / verification

- `uv run pytest -q`: 2,328 passed. The 1 warning is the `discord` package's `audioop` deprecation. The new
  cases were run on the pre-fix code first: each false-positive regression is excluded by it, and each
  over-correction case is kept by an intermediate version.
- The live run above. Nothing was deployed, restarted or written.

## Follow-ups

- `YEARS` does not match "5+ years of C++ experience": `+` is not allowed in the words between "years" and
  "experience". This leads to missed exclusions, not false ones. Not changed here.
- Rules that judge by sentence still miss some wordings, for example "Be either a U.S. citizen or Permanent
  Resident" (an imperative list item) and "Work Authorization: Green Card Holders (GC) & US Citizens (USC)".
- Public Trust is not treated as a clearance. Change `CLEARANCE` if the owner wants Public Trust roles excluded.
