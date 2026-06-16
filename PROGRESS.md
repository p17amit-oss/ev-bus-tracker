# PROGRESS — ev-bus-tracker

> Handoff state for a fresh session. Confirmed against the repo on branch
> `feature/v2-data-foundation`. Read this top-to-bottom before touching anything.

> **Employment-context constraint (read first).** This project is built on
> **personal time, personal devices, and personal accounts only**, using
> **public data only**, and kept **low-profile while employed**. Before any
> launch, monetization, or public promotion: **consult an employment lawyer**
> regarding IP-assignment and moonlighting clauses. Treat this as a gate on
> anything outward-facing, not a footnote.

## Project

India EV Bus Intelligence Tracker (**ev-bus-tracker.pages.dev**) — a
verification-first procurement intelligence product for India's electric-bus
tenders. The audience is **OEM sales teams and EV investors**. The value
proposition is **traceable, honestly-bounded data, not confident answers**:
every claim carries its source, confidence, and coverage boundary, and the
product would rather abstain or flag a conflict than guess. Scope is
**deliberately narrow — e-bus procurement only**, NOT the broader EV market.
Charging-infrastructure and 2-wheeler expansion were explicitly considered and
**rejected for now** to keep the corpus deep rather than wide.

## Architecture principles (non-negotiable)

- **Claim grammar.** Be strong on **positive** evidence *with a source*
  ("X buses under scheme Y per [source], [date]"). Be cautious on **negative**
  evidence: phrase as **"not found in [sources] as of [date]"** — **never**
  "does not exist". Absence of evidence is reported as a coverage statement,
  not a fact about the world.
- **Source / confidence / coverage constraints.** Every fact ties to a
  `source_key` in the `source_coverage` registry. Confidence uses the
  controlled vocabulary (`confirmed | reported | estimated | inferred`) and is
  **never inflated** to look better. Coverage boundaries are recorded per
  record so the product can always say what it does and does not cover.
- **The architecture is NOT the moat.** Anyone can rebuild the schema. The moat
  is the **accumulated corpus**, the **document version graph**, the
  **coverage map**, and the **un-backfillable state-changes** captured over
  time (corrigenda, extensions, withdrawals you can only see if you were
  watching when they happened).
- **Decisive metric.** **Expert-minutes per trusted output must fall over
  time.** If producing one verified, source-backed answer keeps costing the
  same human effort, the instrument isn't working — that ratio is the thing to
  drive down.

## What's built (current state)

### v2 schema — 19 tables (`db/schema.sql`)

`organizations`, `tender_groups`, `tenders`, `tender_events`, `tender_lots`,
`bids`, `deployments`, `registrations`, `charging_events`, `announcements`,
`scrape_runs`, `source_coverage`, `grouping_suggestions`, `documents`,
`document_diffs`, `claims`, `claim_reviews`, `dangling_references`,
`org_aliases`.

The v2-introduced tables and their jobs:

- **`source_coverage`** — the source registry. One row per source
  (`cesl`, `bse`, `vahan`, `cppp`, `dtc`, `best`, `bmtc`, `apsrtc`, `tsrtc`,
  `lok_sabha`, `press`, `user_report`, `gem`, `state_eproc`), each with a
  coverage grade, ingest mode, crawl status, and **known_gaps** — the single
  home for "what we cover and what we don't."
- **`tender_groups`** — grouping container so multi-source observations of the
  same procurement can be **clustered** (via `tenders.group_id`) without ever
  being collapsed into one row.
- **`tender_lots`** — per-city decomposition of a (usually multi-city) tender.
  Single-city tenders need no lot rows.
- **`grouping_suggestions`** — non-destructive **review queue** for ambiguous
  matches that don't clear the auto-group bar.
- **`documents`** — captured source documents (PDFs etc.), archived on capture.
- **`document_diffs`** — the **version graph**: section-level diffs between
  document versions, so corrigenda that overwrite originals are still visible.
- **`claims`** — atomic factual assertions with provenance.
- **`claim_reviews`** — human/cross-source review records over claims.
- **`dangling_references`** — the **conflict / unresolved-reference ledger**.
  Conflicts are logged here (status `conflict`/`unresolved`/...) **instead of**
  being silently resolved or overwritten.
- **`org_aliases`** — entity resolution (migrated out of an
  `organizations.aliases` JSON blob).

### Idempotent schema + seed split (`db/`)

- **`schema.sql`** — idempotent **by contract**: every statement is
  `CREATE ... IF NOT EXISTS`, no `ALTER TABLE`, no data backfill. It runs on
  **every** `get_db()` connection against the live DB, so it must be a no-op on
  an existing DB and must build the full v2 shape on a fresh one.
- **`seed.sql`** — reference/seed data (e.g. the `source_coverage` registry),
  kept **separate** from schema so structure and data never entangle.
- **`migrate_v1_to_v2.sql`** — the **one-time** migration (the only place
  `ALTER TABLE` and backfill `INSERT`s live). Already applied to the committed
  DB.

Why the split: a connection-time `schema.sql` can never clobber migrated data,
and the one-time migration can never accidentally re-run.

### Option C — fact / editorial split (the publish pipeline)

```
DB (data/evbus.db)
  └─ pipeline/export_json.py  ──► site/src/data/tenders_facts.json   (machine facts ONLY)
                                  site/src/data/tenders_editorial.json (hand-authored prose, keyed by tender_ref)
  └─ pipeline/merge_site_data.py ──► site/src/data/tenders.json        (facts + editorial joined)
                                       └─ astro build ──► dist/
```

- `export_json.py` emits **only machine-derivable facts** and is forbidden
  (hard safety rail) from writing `tenders.json` or `tenders_editorial.json`.
- Editorial judgment (`why_it_matters`, `key_risks`, `eligibility_summary`,
  `notes`, `tags`, clean `display_title`) is **hand-authored** in
  `tenders_editorial.json`, keyed by `tender_ref`.
- `merge_site_data.py` joins facts (spine) + editorial (attach by ref) into
  `tenders.json`, which is the only file the Astro site reads.

### Status model

- The **DB stores raw status** derived by a trigger
  (`trg_tenders_status_from_events`, `AFTER INSERT ON tender_events`): e.g. an
  `issued` event sets `status='announced'`, `awarded`/`loa_issued` → `awarded`,
  etc. Because the trigger is INSERT-only, event edits use a
  **delete-and-reinsert** pattern (preserving `dedupe_key`) so status
  re-derives correctly.
- The **site derives the displayed badge** at build time via
  `effectiveStatus()` in `site/src/pages/tenders/`: it ignores `announced` and
  computes from the deadline — `open` / `closing_soon` (≤7 days) /
  `needs_review` (deadline passed, not `extended`) / pass-through for
  `awarded`/`cancelled`/`extended`.

### Lot decomposition (surfaced end-to-end)

Multi-city tenders carry a `tender_lots` breakdown → exported as a plural
`lots` array on each tender fact → passed through merge → rendered on the
tender detail page (`site/src/pages/tenders/[slug].astro`) as a **"Lot
Breakdown"** section. The section renders **only** when `lots` is non-empty
(no empty header for tenders without a breakdown), shows city / bus_count /
scheme (display label, not raw enum), surfaces each lot's `coverage_boundary`
as an understated provenance tooltip, and prints a **Σ-check line**: a quiet
confirmation when the lot bus_counts sum to the headline `bus_count`, and a
visible amber flag if they ever diverge — a self-checking display.

## Standing decisions (do not re-litigate)

- **Clustering, not merge.** Multiple source observations of the same
  procurement **cluster** via `group_id`; they are **never collapsed** into a
  single row. Provenance per observation is preserved.
- **Four-signal auto-group rule.** Auto-group only when **all four** hold:
  scheme **exact** match + city overlap + bus_count within **2%** + dates
  consistent. **3-of-4 → review queue** (`grouping_suggestions`), never
  auto-grouped.
- **Strict for multi-city tenders.** No relaxed auto-grouping. Grouping happens
  at the **lot level only**, and **only after PDF extraction** gives reliable
  per-lot data.
- **Source authority order:** issuing **STU > CESL > CPPP > press > Vahan**.
  Higher authority wins on conflict (but conflicts are still logged, not
  silently overwritten).
- **Confidence stays honest.** Single-source = `reported` / "medium". It is
  **not** bumped to look more authoritative than the evidence supports.
- **Never fabricate to fill a gap.** Missing or conflicting data is recorded in
  `dangling_references` and reported as a coverage/conflict statement — never
  invented.

## Shakedown status (the validation gate)

The 7-day shakedown validates the **instrument** against the two CESL tenders
before any new source is added. **Full Day-7 validation record:
[SHAKEDOWN.md](SHAKEDOWN.md).**

**Status: complete — all three tests resolved, Day-7 decision recorded.**

**Days 1–2 — claims + corrections (done):**
- Atomic claim list built and **verified** for both CESL tenders (DB vs
  `tenders.json` cross-check, raw_text provenance review).
- Corrections applied as a reviewed batch (committed; DB backed up to the
  gitignored `data/evbus.db.pre_shakedown.db`):
  - **T2** fabricated `issue_date` `2026-01-01` → sourced **`2026-01-19`**
    (documented IFB date; issued event re-dated via delete-and-reinsert).
  - **T2** `bus_count` `2900` → **`6230`** (2,900 PM E-DRIVE + 3,330 Delhi)
    with a **5-city lot breakdown** (Delhi 3330, Mumbai 1500, Pune 1000,
    Ahmedabad 200, Hyderabad 200 — sums to 6230).
  - **T2** bid-deadline conflict (DB `2026-04-15` vs press `2026-03-10`,
    opening `2026-02-23`) **flagged in `dangling_references`, NOT resolved** —
    needs the **primary CESL document** to settle.
  - **T1** unsourced `prebid_date` **nulled** (was a copy of `issue_date`).
  - Both tenders' `is_multi_city` corrected.

**Gate tests (all resolved — see [SHAKEDOWN.md](SHAKEDOWN.md) for detail):**
1. **Verifier — NOT PASSING (gap documented).** `documents`/`document_diffs`
   modeled but empty; `is_current` defaults to 1, no trigger flips superseded
   docs, exporter/site never filter on it. Guarantee true only by emptiness.
   Must be closed atomically as part of the diff-engine build (cleanup item b).
2. **Abstention — PASS (structural).** Static source-display product, no
   query/prediction/eligibility surface; `eligibility_summary` renders sourced
   criteria, not verdicts. Standing editorial rule recorded.
3. **Coverage-honesty — FAILED → FIXED.** Methodology page overclaimed
   (Vahan/GeM/5 STUs/DIMTS shown active) vs registry reality; §1/§2/§6 rewritten
   to match `source_coverage` (commit `ca62f29`). Residual: `last_crawled_at`
   null on every row — resolves with real timestamped scrape runs (cleanup item e).

**Day-7 decision:** instrument is trustworthy for the two live CESL tenders with
the above gaps documented. Verifier gap + `coverage.json` + `last_crawled_at`
must be closed before/alongside scaling. **Proceed to the cleanup batch, then
CPPP — not before.**

## Open items / known debt (flagged, not yet fixed)

- **Merge compatibility layer — DONE (cleanup item c).** The alias block in
  `merge_site_data.py` was removed; the Astro pages now read native field names
  directly, scheme labels live in `site/src/lib/labels.ts`, and titles use
  `display_title ?? title`. Merge now only copies the facts spine, parses
  states/cities to arrays, attaches editorial, and guarantees `lots[]`.
- **Exporter should emit a clean title** so `title` is never garbage and the
  `display_title ?? title` fallback in the pages becomes unnecessary. (Follow-on
  to the compat-layer removal — currently the raw scraped title is still junk and
  the clean title only exists in editorial.)
- **Methodology coverage data-driven — DONE (cleanup item a).** `export_json.py`
  emits `coverage.json` from `source_coverage`; methodology §1 renders the
  active/planned split from it (a `planned` row cannot render as active), and the
  §2/§6 source lists derive from the active set. Can't drift from the registry.
- **Add a `display_name` column to `source_coverage`** so methodology prose reads
  naturally (e.g. "BSE corporate filings") instead of the raw `source_name`
  ("BSE Corporate Announcements"). Follow-on to making the page data-driven —
  avoids re-introducing hand-maintained strings in the page.
- **`BUILD_DATE` real-date + daily rebuild — DONE (cleanup item d).** All five
  tender-consuming pages now derive `BUILD_DATE` from the real build date
  (`new Date(new Date().toISOString().slice(0,10))`, UTC-midnight), so
  `effectiveStatus()` reflects today. CI part: `scrape.yml` now regenerates the
  site JSON and `daily-rebuild.yml` forces a Cloudflare rebuild daily so the
  date can't freeze on no-data days. **PENDING manual step:** set the
  `CF_PAGES_DEPLOY_HOOK` repo secret (Cloudflare Pages deploy-hook URL) in
  GitHub settings — `daily-rebuild.yml` fails fast until it exists.
- **Gap-1 (found + fixed during item d):** CI never regenerated the site JSON
  from the DB — the daily scrape committed `data/evbus.db` but not
  `tenders_facts.json` / `tenders.json` / `coverage.json`, so scraped changes
  never reached the site (the Astro build reads the JSON, not the DB).
  `scrape.yml` now runs `export_json.py` + `merge_site_data.py` before
  committing and stages the JSON, gating the commit on the JSON so SQLite WAL
  churn on `data/evbus.db` can't produce empty-data commits.
- **`last_crawled_at` real — DONE (cleanup item e).** Root cause: `track_run()`
  never wrote `scrape_runs.source_key`, so the freshness trigger's
  `NEW.source_key IS NOT NULL` guard always failed. Fixed by giving `track_run`
  an explicit `source_key` param (each scraper passes its own — bse/cesl/vahan),
  written on INSERT and re-asserted on the status UPDATE so the trigger fires.
  Backfilled the existing runs into `source_coverage` (bse/cesl `ok` with real
  dates; vahan honestly `failed` at its last attempt). Going forward the trigger
  refreshes `last_crawled_at` automatically and the daily export carries it to
  `coverage.json`. Standing caveat: manual sources (`press`, `user_report`) have
  no scrape runs, so they legitimately show no crawl date.
- **Garbage scraped titles.** The scraped `title` (DB / `tenders_facts.json`)
  is raw PDF-listing junk; the clean titles users see are **hand-authored
  editorial**. Honest, but **not root-fixed** — the scraper needs to extract
  better titles.
- **Binary DB commit reviewability.** `data/evbus.db` is committed as a binary
  blob, so diffs are opaque in review. **Consider committing a SQL dump
  alongside** for readable diffs.

## Next steps (in order)

1. **Finish the remaining shakedown tests** (verifier, abstention,
   coverage-honesty) and **write the shakedown result doc** (Day-7 record).
2. **Cleanup batch:** remove the merge compat layer (migrate pages to native
   fields), fix `BUILD_DATE` + daily rebuild, decide on the SQL-dump-alongside.
3. **THEN** the **CPPP scraper + grouping pipeline** — the first source that
   actually exercises multi-source grouping.
4. **Then the four STU portals** (DTC, BEST, BMTC, APSRTC/TSRTC), **one at a
   time**, each with its **methodology row written before ingestion**.
   - **GeM** and **state e-procurement** portals are **explicitly deferred**
     (login-gated / DSC-gated / bot-defended).

**The 7-day shakedown gates scaling. Do not add sources until the instrument
is validated.**

## Git state

- Working branch: **`feature/v2-data-foundation`** — **not merged to `main`,
  not deployed**.
- `main` is still at the **v1 rebuild** (`b31c2bd feat: v1 rebuild —
  intelligence-led tracker with full data model and all pages`).

Commits on the branch (oldest → newest), ahead of `main`:

```
276f2e0 schema: v2 data foundation — migration, idempotent schema+seed split
2044ad6 pipeline: Option C fact/editorial split (DB -> facts -> merge -> site)
6d32699 data(site): editorial source + regenerated facts/merged site data
3ee7db3 data: v2 migration applied + dated events + factual backfill
3da44cb chore: gitignore content-pipeline venv and pycache
3823548 data: apply CESL tender shakedown corrections (verified batch)
3b418bf pipeline: surface tender_lots to the site facts/merge layer
aed3b39 site: lot-breakdown UI on tender detail page
```

(Plus this `docs:` commit for PROGRESS.md.)
