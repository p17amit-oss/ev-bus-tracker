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

**Secondary gap (not yet closed):** `last_crawled_at` is **null on every
registry row**, including the live sources. So even CESL/BSE cannot *evidence*
freshness — the page honestly states cadence ("automated, daily") but cannot
assert a proven last-crawl timestamp. This resolves only when the
coverage-freshness trigger fires on a **real, timestamped scrape run**.

## Permanent fixes deferred to the cleanup batch

These are explicitly out of scope for the shakedown hand-edits and belong to the
cleanup batch (see [PROGRESS.md](PROGRESS.md)):

- **(a)** Drive the methodology page from an exported **`coverage.json`**
  generated from `source_coverage`, so the coverage description **can never
  drift** from the registry again.
- **(b)** The **`is_current` enforcement** from Test 1 (atomic supersede +
  filtered reads + visible history).
- **(c)** Remove the **merge compatibility layer** (migrate Astro pages to
  native field names).
- **(d)** **`BUILD_DATE` → real date** in the tender pages + ensure a **daily
  rebuild** (status currently anchored to build time, not real time).
- **(e)** **Populate `last_crawled_at`** via real scrape runs so freshness is
  evidenced, not assumed.

## Day-7 Decision

The instrument is **trustworthy for the two live CESL tenders**, with the gaps
above documented and bounded:

- **Test 1 (Verifier)** — NOT PASSING; gap documented, true-by-emptiness today.
- **Test 2 (Abstention)** — PASS (structural), with a standing editorial rule.
- **Test 3 (Coverage honesty)** — FAILED then FIXED; one secondary freshness
  gap pending real scrape timestamps.

**The verifier gap (b) and the coverage.json (a) + `last_crawled_at` (e) items
must be closed before or alongside scaling.**

**Order of operations:** proceed to the **cleanup batch**, then the **CPPP
scraper + grouping pipeline** — **not before**. Do not add new sources until the
deferred items that gate trust are closed.
