# Shared country preferences — 2026-10-10

**Step:** 2, 4   **Status:** implemented and deployed; Linux regression and live queries verified

The user's Canada exclusion now has one dedicated file, `config/preferences.json`, independent of website search acquisition and semantic filter folders. A matching Canadian location hides the entire job, including mixed US/Canada listings. Evidence is preserved; no manual decisions or recommendation states are rewritten.

`src/auto_app/preferences.py` is standard-library-only and shared by the SQLite read service and DbStore display/selection paths. It checks all postings' raw locations as well as merged/archived locations, recognizes Canadian provinces, and preserves California `CA` and `Ontario, CA`. Ordinary CLI/TUI/web lists and model planning honor it; audit/evaluation and direct details retain access. Read lists filter before counts/pagination. Preferences are included in cursor identity. `context` reports raw and preference counts separately; each read item includes screening reasons. The read CLI/API supports `--include-excluded` and an explicit `--preferences` override.

The example is public; actual preferences are Git-ignored. Clearing `exclude_countries` removes the restriction on the next query or filter plan. Countries beyond Canada currently require explicit names in location text; this does not implement complete feasibility screening.

Verification: failing tests first demonstrated missing screening, raw remote-country leakage, model runs on excluded jobs and absent display filtering. Focused regressions cover geography false positives, mixed locations, non-primary evidence, pagination/cursor changes, read-only data preservation, HTTP, a `python -S` script, CLI/TUI display, invalid config and model planning. Linux full-suite and production read-only verification are recorded after deployment.

Live verification found `New Brunswick, NJ` falsely matching a Canadian province name. Regression tests reproduced it; the matcher now disambiguates explicit US states while preserving Canadian province codes and explicit Canadian evidence in mixed locations. The final Ubuntu suite passed **678 tests, 1 skipped, in 273.65 seconds**. The skip is the existing unbuilt web frontend smoke test.

The active Linux file is `/home/postedism/Desktop/job-search/runtime/config/preferences.json`. The 2026-10-10 21:03 Los Angeles snapshot retains **7,840 raw jobs**, excludes **463**, and displays **7,377**; **7,335** displayed jobs have active source evidence. All pages were traversed with the standard-library query service; an independent raw-location check found no explicit-Canada job remaining. `New Brunswick, NJ` remains visible. The production worker stays active and both public sources report `ok`.

Artifacts on Linux: `checks/country-preferences-full-tests.log`, `checks/country-preferences-manifest.json`, `checks/country-preferences-live-summary.json`, `checks/country-preferences-excluded-jobs.json`, and `checks/jobs-without-canada.csv`. The summary, exclusions and CSV are also in the local workspace's `reports/2026-10-10/`. Original exports remain intact. No Git commit or push was made.
