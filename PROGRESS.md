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

### Scrapers + grouping pipeline (wired to production)

- **CESL** (`scrapers/cesl.py`) — original source; carries the two live tenders.
- **CPPP** (`scrapers/cppp.py`) — Active Tenders + Corrigendum feeds, bus-filtered,
  `source_key='cppp'`; runs daily in `scrape.yml`; confirmed executing live
  (scrape_runs row written, `last_crawled_at` refreshed). Zero bus rows in the
  current rolling window.
- **DTC / Delhi NIC** (`scrapers/dtc.py`) — **built, tested against real live NIC
  HTML** (feed-scoped parser over the real GePNIC markup fixture in
  `tests/test_dtc.py`), **wired into `scrape.yml`**, and **confirmed executing
  live** (scrape_runs row written, `last_crawled_at` updated, `crawl_status` ok).
  **Zero bus rows in the current window** — the dedupe key (normalized ref+title)
  and issuer resolution are **untightened against real bus data** and will
  auto-flag via `dangling_references` on first capture. **Corrigendum feed
  dropped**: the Latest-Corrigendums feed uses issuer NIT refs, a non-bridging
  namespace vs the Latest-Tenders feed's GePNIC system codes, so corrigenda can't
  be linked to their parent tender by ref — documented in
  `source_coverage.dtc.known_gaps`.
- **Grouping pipeline** (`pipeline/group_tenders.py`) — four-signal detection +
  `--apply` + `--fixture`; live run is a clean no-op (both CESL tenders are
  same-source). Lot-level multi-city grouping stubbed pending PDF extraction.

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
  - **T2** bid-deadline conflict — **RESOLVED 2026-06-28**: deadline confirmed
    **`2026-03-10`** per CESL portal + Sustainable Bus + electrive (3 independent
    sources); the `2026-04-15` value was erroneous. `dangling_references` row
    `id=1` set to `resolution_status='resolved'`. No longer an open item.
  - **T1** unsourced `prebid_date` **nulled** (was a copy of `issue_date`).
  - Both tenders' `is_multi_city` corrected.

**Gate tests (all resolved — see [SHAKEDOWN.md](SHAKEDOWN.md) for detail):**
1. **Verifier — NOT PASSING (gap documented, deferred to diff engine).**
   `documents`/`document_diffs` modeled but empty; `is_current` defaults to 1,
   no trigger flips superseded docs, exporter/site never filter on it. Guarantee
   true only by emptiness. **Confirmed NOT a CPPP blocker:** CPPP captures
   corrigendum *events* (deadline extensions, amended terms) as `tender_events`
   rows — handled by the existing status trigger — but does **not** populate
   `documents` / `document_diffs` (it doesn't expose corrigendum PDFs without
   deeper scraping). With no documents there is nothing to supersede, so the
   `is_current` gap stays genuinely dormant through CPPP. Item (b) is gated by
   the **diff-engine build** (the feature that ingests documents and can
   supersede them), closed atomically there.
2. **Abstention — PASS (structural).** Static source-display product, no
   query/prediction/eligibility surface; `eligibility_summary` renders sourced
   criteria, not verdicts. Standing editorial rule recorded.
3. **Coverage-honesty — FAILED → FIXED.** Methodology page overclaimed
   (Vahan/GeM/5 STUs/DIMTS shown active) vs registry reality; §1/§2/§6 rewritten
   to match `source_coverage` (commit `ca62f29`). Residual: `last_crawled_at`
   null on every row — resolves with real timestamped scrape runs (cleanup item e).

**Day-7 decision:** instrument is trustworthy for the two live CESL tenders with
the above gaps documented. **Cleanup batch CLOSED** — (a) coverage.json, (c)
compat-layer removal, (d) BUILD_DATE + daily rebuild, (e) last_crawled_at are all
done; (b) verifier gap is confirmed deferred to the diff-engine build (it gates
that feature, not listing-source expansion). **Next active task: CPPP scraper +
grouping pipeline.**

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
  date can't freeze on no-data days. **DONE (2026-06-28):** the
  `CF_PAGES_DEPLOY_HOOK` repo secret is set and confirmed green on manual
  dispatch; `daily-rebuild.yml` fires nightly at 03:00 UTC.
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
- **T2 display bug — RESOLVED 2026-06-28.** T2 had been surfacing an incorrect
  bus count / deadline / status across the homepage feed, charging page, and
  editorial. Corrected to **6,230 buses**, deadline **`2026-03-10`**, with
  editorial prose updated to "submission closed, under bid evaluation, no award
  confirmed." All surfaces verified consistent on the live site.
- **Deploy pipeline — RESOLVED 2026-06-28 (build success ≠ deployment).** The
  Cloudflare **Workers** project (deploy command was `npx astro build` — a
  no-op that only rebuilds) **silently failed to deploy for 17+ days while every
  build showed green**. Deleted 2026-06-28 and replaced by a Cloudflare **Pages**
  project with auto-publish (no deploy command needed). **Lesson: always verify
  the LIVE URL, not just the build log, as the acceptance test.**

## Next steps (in order)

1. **`under_evaluation` status model** — `effectiveStatus()` has no state
   between "deadline passed" and "awarded." T2 badge shows `needs_review` while
   prose correctly says "under bid evaluation." Design entry trigger (bid-opening
   event), exit trigger (award/cancel), and decay rule (how long before a silent
   under-evaluation tender decays to genuine needs_review), then implement
   across: DB event vocab, status trigger, `effectiveStatus()`, badge rendering,
   editorial. **First task next session.**
2. **Notification on first bus capture** — CPPP and DTC run autonomously to
   production. First real bus row (and first cross-source grouping opportunity)
   will land silently. Daily scrape commit message or run log should surface
   "N new tenders, M new dangling_references" so the event is visible.
3. **BEST / BMTC / APSRTC** — next STU portals after DTC, one at a time,
   methodology row before scraper (standing rule).
4. **Lot-level grouping** — stubbed pending PDF extraction → `tender_lots`
   population. The actual bottleneck for grouping on real corpus (both CESL
   tenders are multi-city; auto-group path doesn't apply to them).
5. **Diff engine** — document ingestion + version graph, closes the verifier
   gap (b) atomically.
6. **Backlog** (from external review, do not build on mock data): charging
   page, state rollups, component library — gated on real data existing first.

## Backlog (from external review)

Captured from an external review of the site; none change current sequencing,
all are later considerations:

- **Charging-infra as a first-class page + audience.** Schema has
  `charging_events` but no dedicated page. The idea: surface charging
  intelligence (bundled-into-bus-tender vs standalone, `opportunity_type`
  CPO/EPC/charger-OEM) targeting charging companies as a potential early paying
  audience. In-scope because it's charging bundled into bus procurement, not the
  broader EV market. Consider after STU sources land and there's real charging
  data to show — **do NOT build the page on sample data**.
- **State-level rollup pages.** Per-state execution-gap view: sanctioned vs
  tendered vs awarded vs registered vs operational, plus open tenders / active
  OEMs / charging activity. Good differentiator and SEO surface. Build only when
  the underlying per-state data is real, not sample.
- **Reusable component library.** Formalize `StatusBadge`, `ConfidenceBadge`,
  `SourceChip`, `MetricCard`, `DataTable`, `UpdateTimeline`, `EmptyState`,
  `ComingSoonLock` as shared components when the site UI gets its proper design
  pass. Some exist; this is a consolidation task.

**Standing caution recorded with the backlog:** the external review proposed
building States / Charging / Registrations pages now with sample/mock data. We
reject building presentation breadth on mock data — it's the exact overclaiming
the shakedown caught. Pages get built when their data is real.

## Git state

- Working branch: **`main`** — merged from `feature/v2-data-foundation`
  (merge commit `763a806`). Feature branch no longer exists.
- Deployed via **Cloudflare Pages** (Pages project, not Workers).
  Auto-deploys on every push to main. Old Workers project deleted.
- Live at: https://ev-bus-tracker.pages.dev — verified correct post-merge.
- Deploy hook: `CF_PAGES_DEPLOY_HOOK` GitHub secret set and confirmed green
  on manual dispatch. `daily-rebuild.yml` fires nightly at 03:00 UTC.
