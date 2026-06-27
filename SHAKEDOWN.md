# SHAKEDOWN — 7-Day Validation Record

> **Day-7 validation record** for the ev-bus-tracker instrument. Branch:
> `feature/v2-data-foundation`. This document is the gate between "instrument
> built" and "scale by adding sources." Read alongside [PROGRESS.md](PROGRESS.md).

## Scope

The shakedown validates the **instrument** — the data model, the
fact/editorial pipeline, the status logic, the verifier pathway, the abstention
posture, and the coverage-honesty surface — **against the two live CESL tenders
only**, before any new source is added:

- **Tender 1** — `CESL/06/2026-27/PM-eBus Sewa3/262704003` (PM-eBus Sewa, Tender 3).
- **Tender 2** — `CESL/06/2025-26/PM E-Drive/252601015` (PM E-DRIVE, Tender II / Delhi).

The premise: if the instrument cannot be trusted on two hand-verifiable
tenders, it cannot be trusted at scale. **The shakedown gates scaling — no new
sources until the gaps below are closed or explicitly accepted.**

## Days 1–2 — Claims built and verified

Atomic claim list built for both tenders (deadline, bus count, scheme, status,
source, timeline events), cross-checked DB vs `tenders.json` vs the raw scraped
text, and verified against the CESL sources / authoritative trade press
(Sustainable Bus, electrive, 15–19 Jan 2026). Corrections applied as **one
reviewed batch** (DB backed up to the gitignored
`data/evbus.db.pre_shakedown.db`):

- **T2 `issue_date`**: fabricated `2026-01-01` → sourced **`2026-01-19`** (the
  documented IFB date). The fabricated date appeared nowhere in the scraped raw
  text. Issued event re-dated via delete-and-reinsert so the status trigger
  re-derives; `dedupe_key` preserved.
- **T2 `bus_count`**: `2900` → **`6230`** (the documented single procurement
  total: 2,900 PM E-DRIVE + 3,330 Delhi), with a **5-city lot breakdown**
  (Delhi 3330, Mumbai 1500, Pune 1000, Ahmedabad 200, Hyderabad 200 — sums to
  6230).
- **T2 deadline conflict**: DB `bid_due_date 2026-04-15` vs trade-press bid
  submission `2026-03-10` (opening `2026-02-23`). **Flagged in
  `dangling_references` (status `conflict`), NOT resolved** — needs the
  **primary CESL document** to settle. The stored value was left untouched.
- **T1 `prebid_date`**: unsourced `2026-04-22` (a copy of `issue_date` with no
  independent backing in the raw text) → **nulled**.
- **T1 + T2 `is_multi_city`**: corrected to match documented multi-city scope.

These corrections are committed; the lot breakdown is surfaced end-to-end on
the tender detail page with a Σ-check display.

## Test 1 — Verifier: **NOT PASSING (enforcement gap)**

**Requirement:** a superseded document must never display as current.

**Finding:** the version-graph tables (`documents`, `document_diffs`) are
**modeled but empty** (0 rows each). The guarantee is **unenforced**:

- `documents.is_current` defaults to `1`; **no trigger** flips a superseded
  document to `is_current=0` when its successor is ingested.
- The exporter **never filters on `is_current`** — `amendments_for()` joins
  `document_diffs` to the target document without consulting it.
- **No site layer reads** `is_current` / `supersedes_document_id`.

So "a superseded doc must not show as current" is currently true **only by
emptiness** — there are no documents to get it wrong. The moment real documents
arrive, nothing enforces it.

**REQUIREMENT to close (part of the diff-engine build, done atomically):**

1. Ingesting a corrigendum sets the superseded document's `is_current=0`
   **in the same transaction** (ideally a trigger, mirroring the
   status-from-events pattern).
2. Exporter and site filter **current-document reads on `is_current=1`**, while
   **still showing superseded versions** in the diff / history view (superseded
   ≠ deleted — the version graph is the point).

**SEQUENCING (confirmed): (b) gates the diff engine, NOT CPPP.** Reconnaissance
confirms CPPP publicly surfaces corrigendum **events** per tender (deadline
extensions, amended terms), so it is not strictly "no corrigenda." But CPPP
captures those as `tender_events` rows — handled by the existing status trigger
— and does **not** populate `documents` / `document_diffs` (it doesn't expose
corrigendum PDFs without deeper scraping). With no documents ingested there is
**nothing to supersede**, so the `is_current` gap stays **genuinely dormant**
through CPPP: the version-graph tables remain empty and the guarantee holds
true-by-emptiness exactly as today. This is the deliberate reading of the Day-7
"before or alongside scaling" line — the gap is gated by the **diff-engine
build** (the first feature that actually ingests documents and can supersede
them), which is when real documents first appear. **(b) is confirmed deferred
to the diff engine and is NOT a blocker for CPPP.**

## Test 2 — Abstention: **PASS (structural)**

The product is a **static source-display site with client-side filtering
only**. Verified:

- No search-as-query, no chatbot, no "ask", no LLM endpoint, no `fetch()` to any
  answer service. The tender search is pure client-side row hiding; the
  subscribe form is a stub with no backend.
- **No prediction or eligibility-determination surface** anywhere. Term sweep
  (`will win`, `winner`, `predict`, `recommend`, `should bid`, `your company`,
  `odds`, `probability`, `eligible`, `qualify`) found only neutral sourced
  criteria and product-description prose.
- `eligibility_summary` renders **sourced criteria, not verdicts** — it defers
  to the RFQ ("Qualification criteria specified in RFQ document"), never tells a
  specific reader whether they qualify.

**STANDING EDITORIAL RULE (carry forward):** editorial fields
(`eligibility_summary`, `why_it_matters`, `notes`, `key_risks`, etc.) state
**tender criteria and context only** — **never a verdict about a specific
reader, never a prediction of outcome**. This is the one place abstention is
enforced by discipline rather than by code (the fields are hand-authored), so
it must be held deliberately.

## Test 3 — Coverage honesty: **FAILED → FIXED**

**Failure (as found):** the methodology page overclaimed coverage. Vahan, GeM,
all five STU portals, and a **non-existent "DIMTS" source** were presented as
active / High-confidence / "automated daily," while the `source_coverage`
registry showed only **CESL, BSE, press, and user_report** as live. The page
was **hand-written prose severed from the registry** and **self-contradictory**
(§1 implied broad automation; §6 said "only CESL is automated" — itself also
wrong, since BSE is automated too).

**Fix (applied, committed):** rewrote §1/§2/§6 to match the registry —

- §1 split into **Currently active** (CESL/BSE automated, press/user_report
  manual, honest confidence) vs **Planned, not yet active** (Vahan, CPPP, STU
  portals, parliamentary Q&A as planned; GeM + state e-proc as deferred).
- §2 automated collection = **CESL + BSE only**; Vahan daily-scrape claim
  removed.
- §6 now **agrees with §1**; the "Vahan 1–2 month delay" line moved from a
  current limitation to a future caveat on the planned Vahan row.
- **DIMTS removed.** No unsubstantiated freshness claims.

**Secondary gap — RESOLVED (cleanup item e).** `last_crawled_at` was **null on
every registry row** because `track_run()` never wrote `scrape_runs.source_key`,
so the coverage-freshness trigger's `NEW.source_key IS NOT NULL` guard always
failed. Fixed: `track_run` now takes an explicit `source_key` (each scraper
passes its own), written on INSERT and re-asserted on the status UPDATE, so the
trigger fires; existing runs were backfilled into `source_coverage` (CESL/BSE
show real `ok` crawl dates; Vahan honestly shows `failed` at its last attempt).
Going forward the trigger refreshes the timestamp on every run and the daily
export carries it to `coverage.json`, so the methodology page evidences real
freshness. **Standing caveat:** manual sources (`press`, `user_report`) have no
scrape runs and legitimately show no crawl date — that absence is honest, not a
bug.

## Permanent fixes deferred to the cleanup batch

These are explicitly out of scope for the shakedown hand-edits and belong to the
cleanup batch (see [PROGRESS.md](PROGRESS.md)):

- **(a)** Drive the methodology page from an exported **`coverage.json`**
  generated from `source_coverage`. **DONE** (commit `99a0735`).
- **(b)** The **`is_current` enforcement** from Test 1 (atomic supersede +
  filtered reads + visible history). **DEFERRED to the diff-engine build** — see
  the sequencing note under Test 1. Not a CPPP blocker.
- **(c)** Remove the **merge compatibility layer** (migrate Astro pages to
  native field names). **DONE** (commit `66ccc3d`).
- **(d)** **`BUILD_DATE` → real date** + **daily rebuild**. **DONE** (commits
  `0e1af0d`, `fcad871`); one pending manual step: set the `CF_PAGES_DEPLOY_HOOK`
  secret in GitHub.
- **(e)** **Populate `last_crawled_at`** via real scrape runs. **DONE** (commit
  `7d86fd7`).

**Cleanup batch: CLOSED.** (a), (c), (d), (e) are done; (b) is confirmed
deferred to the diff engine (it gates that feature, not listing-source
expansion).

## Day-7 Decision

The instrument is **trustworthy for the two live CESL tenders**, with the gaps
above documented and bounded:

- **Test 1 (Verifier)** — NOT PASSING; gap documented, true-by-emptiness today.
  Gated by the **diff-engine build**, not by CPPP (CPPP captures corrigendum
  events as `tender_events` but ingests no `documents`/`document_diffs`, so with
  nothing to supersede the gap stays dormant through CPPP).
- **Test 2 (Abstention)** — PASS (structural), with a standing editorial rule.
- **Test 3 (Coverage honesty)** — FAILED then FIXED; secondary freshness gap
  also resolved (item e).

**Refined reading of "before or alongside scaling":** the verifier gap (b) gates
the **diff engine** (the feature that can actually supersede documents), not
listing-source expansion. The coverage.json (a) and `last_crawled_at` (e) items
are done. So nothing blocks the next listing source.

**Order of operations:** the cleanup batch is **closed**. Proceed to the **CPPP
scraper + grouping pipeline** (the next active task). Close the verifier gap (b)
as part of the diff-engine build, when real documents first arrive.
