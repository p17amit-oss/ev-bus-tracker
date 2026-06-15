-- =============================================================================
-- ev-bus-tracker: migration v1 -> v2
-- =============================================================================
-- Run ONCE against the live evbus.db. This is not idempotent: SQLite does not
-- support ALTER TABLE ADD COLUMN IF NOT EXISTS, so re-running will error on the
-- ALTER blocks. If you need to re-run, restore from a pre-migration copy first.
--
--   sqlite3 evbus.db ".backup evbus.pre_v2.db"   # back up first
--   sqlite3 evbus.db < migrate_v1_to_v2.sql
--
-- What this migration establishes:
--   1. Epistemic metadata on every substantive table (confidence,
--      derivation_type, coverage_boundary, verified_by) — the claim grammar.
--   2. source_coverage — the honesty layer; declared, graded source universe.
--   3. Multi-source GROUPING model (clustering, not merge): each source keeps
--      its own intact tender row; tender_groups + group_id cluster observations
--      of the same real procurement without collapsing them.
--   4. tender_lots — decomposition of multi-city tenders so a CESL city lot can
--      group with a STU listing at lot granularity.
--   5. grouping_suggestions — non-destructive review queue for ambiguous matches.
--   6. documents + document_diffs — versioned docs with supersession; the diff
--      engine's evidence layer.
--   7. claims + claim_reviews — the answerability lattice and the expert-minutes
--      instrument (the decisive metric).
--   8. dangling_references — the known-unknowns queue (reference-closure).
--   9. org_aliases — entity resolution, migrated out of the JSON blob.
--  10. tenders.status derived from tender_events via trigger (stop setting it
--      directly in the scraper).
-- =============================================================================

PRAGMA foreign_keys = OFF;   -- off during migration; re-enabled at the end
BEGIN TRANSACTION;


-- =============================================================================
-- SECTION 1 — EPISTEMIC METADATA on existing substantive tables
-- =============================================================================
-- Semantics (documented once, reused everywhere):
--   confidence:        confirmed | reported | estimated | inferred
--   derivation_type:   direct | computed | cross_referenced | inferred
--   coverage_boundary: free text — which sources were checked, as of when
--   verified_by:       NULL (unreviewed) | cross_source | human
--   source_key:        FK-by-convention to source_coverage.source_key
-- -----------------------------------------------------------------------------

-- tenders
ALTER TABLE tenders ADD COLUMN source_key        TEXT;
ALTER TABLE tenders ADD COLUMN confidence        TEXT NOT NULL DEFAULT 'reported'
    CHECK (confidence IN ('confirmed','reported','estimated','inferred'));
ALTER TABLE tenders ADD COLUMN derivation_type   TEXT NOT NULL DEFAULT 'direct'
    CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred'));
ALTER TABLE tenders ADD COLUMN coverage_boundary TEXT;
ALTER TABLE tenders ADD COLUMN verified_by       TEXT
    CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL);
-- Grouping: each tenders row is ONE source-observation. group_id clusters
-- observations of the same single-city procurement. NULL = ungrouped (shows
-- standalone). Multi-city tenders group at lot level (see tender_lots).
ALTER TABLE tenders ADD COLUMN group_id          INTEGER REFERENCES tender_groups(id);
ALTER TABLE tenders ADD COLUMN is_multi_city     INTEGER NOT NULL DEFAULT 0
    CHECK (is_multi_city IN (0,1));
-- scheme as a controlled value — required signal for grouping. Backfill from
-- existing data after migration; default 'unknown' keeps NOT NULL satisfiable.
ALTER TABLE tenders ADD COLUMN scheme            TEXT NOT NULL DEFAULT 'unknown'
    CHECK (scheme IN ('pm_ebus_sewa','pm_edrive','fame_2','state_funded',
                      'smart_city','other','unknown'));

-- tender_events  (per-event provenance; events are append-only)
-- NOTE: SQLite forbids a non-constant default (datetime('now')) in ADD COLUMN,
-- so captured_at is added nullable and backfilled below; the application must
-- set captured_at on every new insert (the scraper already knows capture time).
ALTER TABLE tender_events ADD COLUMN source_key        TEXT;
ALTER TABLE tender_events ADD COLUMN captured_at       TEXT;
ALTER TABLE tender_events ADD COLUMN archived_path     TEXT;
ALTER TABLE tender_events ADD COLUMN confidence        TEXT NOT NULL DEFAULT 'reported'
    CHECK (confidence IN ('confirmed','reported','estimated','inferred'));
ALTER TABLE tender_events ADD COLUMN derivation_type   TEXT NOT NULL DEFAULT 'direct'
    CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred'));
ALTER TABLE tender_events ADD COLUMN coverage_boundary TEXT;
ALTER TABLE tender_events ADD COLUMN verified_by       TEXT
    CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL);

-- bids
ALTER TABLE bids ADD COLUMN source_key         TEXT;
ALTER TABLE bids ADD COLUMN consortium_members TEXT;   -- JSON array of org ids
ALTER TABLE bids ADD COLUMN price_basis        TEXT;   -- with_electricity | ex_electricity | unspecified
ALTER TABLE bids ADD COLUMN escalation_terms   TEXT;
ALTER TABLE bids ADD COLUMN confidence         TEXT NOT NULL DEFAULT 'reported'
    CHECK (confidence IN ('confirmed','reported','estimated','inferred'));
ALTER TABLE bids ADD COLUMN derivation_type    TEXT NOT NULL DEFAULT 'direct'
    CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred'));
ALTER TABLE bids ADD COLUMN coverage_boundary  TEXT;
ALTER TABLE bids ADD COLUMN verified_by        TEXT
    CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL);

-- deployments
ALTER TABLE deployments ADD COLUMN source_key        TEXT;
ALTER TABLE deployments ADD COLUMN confidence        TEXT NOT NULL DEFAULT 'reported'
    CHECK (confidence IN ('confirmed','reported','estimated','inferred'));
ALTER TABLE deployments ADD COLUMN derivation_type   TEXT NOT NULL DEFAULT 'direct'
    CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred'));
ALTER TABLE deployments ADD COLUMN coverage_boundary TEXT;
ALTER TABLE deployments ADD COLUMN verified_by       TEXT
    CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL);

-- charging_events
ALTER TABLE charging_events ADD COLUMN source_key        TEXT;
ALTER TABLE charging_events ADD COLUMN confidence        TEXT NOT NULL DEFAULT 'reported'
    CHECK (confidence IN ('confirmed','reported','estimated','inferred'));
ALTER TABLE charging_events ADD COLUMN derivation_type   TEXT NOT NULL DEFAULT 'direct'
    CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred'));
ALTER TABLE charging_events ADD COLUMN coverage_boundary TEXT;
ALTER TABLE charging_events ADD COLUMN verified_by       TEXT
    CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL);

-- registrations  (backfillable source; lighter metadata, but tag the source)
ALTER TABLE registrations ADD COLUMN source_key TEXT NOT NULL DEFAULT 'vahan';

-- announcements  (BSE staging; add triage_result + source_key)
ALTER TABLE announcements ADD COLUMN source_key    TEXT NOT NULL DEFAULT 'bse';
ALTER TABLE announcements ADD COLUMN triage_result TEXT
    CHECK (triage_result IN
      ('order_confirmed','deployment_update','capex_signal',
       'policy_commentary','tender','charging','boilerplate','unclear')
      OR triage_result IS NULL);

-- scrape_runs  (richer run accounting for the health digest)
ALTER TABLE scrape_runs ADD COLUMN source_key            TEXT;
ALTER TABLE scrape_runs ADD COLUMN rows_updated          INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scrape_runs ADD COLUMN dangling_created      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scrape_runs ADD COLUMN suggestions_created   INTEGER NOT NULL DEFAULT 0;


-- =============================================================================
-- SECTION 2 — source_coverage (the honesty layer)
-- =============================================================================
CREATE TABLE IF NOT EXISTS source_coverage (
    id                  INTEGER PRIMARY KEY,
    source_key          TEXT NOT NULL UNIQUE,
    source_name         TEXT NOT NULL,
    source_type         TEXT CHECK (source_type IN
                          ('portal','exchange','registry','aggregator',
                           'press','govt_notification','parliamentary','user')),
    coverage_grade      TEXT NOT NULL DEFAULT 'C'
                        CHECK (coverage_grade IN ('A','B','C')),
    -- A: structured, complete, auditable (CESL, BSE)
    -- B: good on major events, spotty on smaller/later-stage (STU pages, press)
    -- C: best-effort, gaps expected and documented (aggregators, user reports)
    ingest_mode         TEXT NOT NULL DEFAULT 'planned'
                        CHECK (ingest_mode IN ('automated','manual','planned')),
    coverage_start_date TEXT,
    last_crawled_at     TEXT,
    crawl_status        TEXT NOT NULL DEFAULT 'never'
                        CHECK (crawl_status IN
                          ('ok','partial','failed','blocked','never')),
    known_gaps          TEXT,
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Seed the declared source universe. Grades and modes are HONEST as of build
-- start: live sources = automated; planned sources = planned/never. Do not mark
-- a source 'automated' until its scraper actually runs. The methodology page
-- must render this table, not hardcoded prose.
INSERT OR IGNORE INTO source_coverage
  (source_key, source_name, source_type, coverage_grade, ingest_mode, crawl_status, known_gaps)
VALUES
  ('bse',     'BSE Corporate Announcements',          'exchange',     'A','automated','ok',
   'Retention window ~2 years; older filings need manual backfill.'),
  ('cesl',    'CESL Tender Portal',                   'portal',       'A','automated','ok',
   'Corrigenda can overwrite originals; documents are archived on capture to mitigate.'),
  ('vahan',   'Vahan Analytics Dashboard',            'registry',     'A','planned','never',
   'Scraper in progress. State data revises ~3 months; trailing months re-captured.'),
  ('cppp',    'CPPP (eprocure.gov.in)',               'aggregator',   'B','planned','never',
   'Broad discovery, weak on corrigenda/awards which live on issuer portals. Not yet indexed.'),
  ('dtc',     'Delhi Transport Corporation tenders',  'portal',       'B','planned','never',
   'STU notice page. Bid submission runs on Delhi e-proc (DSC-gated, not scraped). Not yet indexed.'),
  ('best',    'BEST (Mumbai) tenders',                'portal',       'B','planned','never',
   'Several BEST tenders run via Mahatenders / CESL lots. Not yet indexed.'),
  ('bmtc',    'BMTC (Bengaluru) tenders',             'portal',       'B','planned','never',
   'Karnataka e-proc carries bid submission. Not yet indexed.'),
  ('apsrtc',  'APSRTC (Andhra Pradesh) tenders',      'portal',       'B','planned','never',
   'Not yet indexed.'),
  ('tsrtc',   'TSRTC (Telangana) tenders',            'portal',       'B','planned','never',
   'Not yet indexed.'),
  ('lok_sabha','Lok Sabha / Rajya Sabha Q&A',         'parliamentary','B','planned','never',
   'High-credibility sanction/deployment figures. Keyword-triggered; pre-2022 not indexed.'),
  ('press',   'Trade press (ET Auto, Mercom, etc.)',  'press',        'B','manual','ok',
   'Good for large awards; smaller state tenders and corrigenda coverage incomplete.'),
  ('user_report','User-reported source',              'user',         'C','manual','ok',
   'Submitted via the report form; surfaced as user-reported in the source trail when resolved.'),
  ('gem',     'GeM (Government e-Marketplace)',        'aggregator',   'C','planned','never',
   'DEFERRED. Login-gated, bot-defended. Explicitly a known coverage gap for now.'),
  ('state_eproc','State e-procurement portals',       'portal',       'C','planned','never',
   'DEFERRED. DSC-gated bid systems (Mahatenders, Delhi/Karnataka/AP/TS e-proc). Known gap.');


-- =============================================================================
-- SECTION 3 — multi-source GROUPING (clustering, not merge)
-- =============================================================================
-- A tender_groups row is the IDENTITY of one real-world procurement. Member
-- observations (whole single-city tenders, or lots of multi-city tenders) point
-- at it via group_id. Members are NEVER collapsed — the group view renders each
-- member's record and timeline side by side, attributed to its source.
--
-- representative_* designates the single count-bearing member so aggregate bus
-- counts sum over groups without double-counting. Chosen by source authority
-- (see AUTO-GROUP RULE below). This is the ONLY place a canonical value is
-- selected, and only for numeric aggregation — never for display.
-- =============================================================================
CREATE TABLE IF NOT EXISTS tender_groups (
    id                       INTEGER PRIMARY KEY,
    canonical_label          TEXT NOT NULL,         -- e.g. 'PM E-DRIVE Delhi 3,330-bus GCC'
    scheme                   TEXT NOT NULL DEFAULT 'unknown',
    primary_city             TEXT,
    representative_member_type TEXT CHECK (representative_member_type IN ('tender','lot')),
    representative_member_id  INTEGER,              -- id in tenders or tender_lots
    representative_bus_count  INTEGER,              -- count used for aggregation
    confidence               TEXT NOT NULL DEFAULT 'reported'
                             CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    verified_by              TEXT
                             CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL),
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

-- tender_lots: decomposition of a (usually multi-city) tender observation into
-- its city lots. Single-city tenders need no lot row — they group at tenders
-- level via tenders.group_id. Multi-city tenders get one lot per city, and each
-- lot carries its own group_id so a CESL Delhi lot can cluster with the DTC
-- Delhi listing. Lot decomposition depends on PDF extraction; until that runs,
-- multi-city tenders stay ungrouped (is_multi_city=1, no lots) and route to the
-- grouping_suggestions queue. THIS IS THE DELIBERATE 'strict for multi-city'
-- policy: no relaxed auto-grouping for multi-city tenders.
CREATE TABLE IF NOT EXISTS tender_lots (
    id              INTEGER PRIMARY KEY,
    tender_id       INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    lot_label       TEXT,                           -- issuer's lot/package label
    city            TEXT,
    state           TEXT,
    scheme          TEXT NOT NULL DEFAULT 'unknown',
    bus_count       INTEGER,
    bus_length_m    TEXT,
    group_id        INTEGER REFERENCES tender_groups(id),
    confidence      TEXT NOT NULL DEFAULT 'reported'
                    CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    derivation_type TEXT NOT NULL DEFAULT 'direct'
                    CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred')),
    coverage_boundary TEXT,
    verified_by     TEXT CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ----------------------------------------------------------------------------
-- AUTO-GROUP RULE (spec for the pipeline; enforced in Python, not SQL)
-- ----------------------------------------------------------------------------
-- Auto-create a group / attach a member ONLY when ALL FOUR signals match
-- (over-determined). Anything matching 3 of 4 -> grouping_suggestions (pending),
-- never auto-grouped.
--   1. scheme        : exact match on the controlled value
--   2. city/lot      : at least one shared city
--   3. bus_count     : equal, or within 2% (rounding/variation clauses)
--   4. dates         : issue_date OR bid_due_date within a short window
-- Multi-city tenders (is_multi_city=1): NO auto-group at tender level. Decompose
-- to lots first; group lot-to-lot under the same four-signal rule. Until lots
-- exist, route to grouping_suggestions only.
-- Source authority order (for representative_member selection and tie-breaks):
--   issuing STU portal (for its own city tender) > CESL > CPPP > press > vahan.
--   For CESL-aggregated multi-city procurements, the CESL lot and the STU lot
--   are co-equal observations; representative_member = the one with more fields
--   populated, tie -> CESL.
-- ----------------------------------------------------------------------------

-- grouping_suggestions: non-destructive review queue. A wrong call here is a
-- display issue, fixable by editing a row — not data corruption.
CREATE TABLE IF NOT EXISTS grouping_suggestions (
    id              INTEGER PRIMARY KEY,
    member_a_type   TEXT NOT NULL CHECK (member_a_type IN ('tender','lot')),
    member_a_id     INTEGER NOT NULL,
    member_b_type   TEXT NOT NULL CHECK (member_b_type IN ('tender','lot')),
    member_b_id     INTEGER NOT NULL,
    signals_matched TEXT,                           -- JSON: which of the 4 matched
    match_score     REAL,                           -- 0..1, advisory
    suggested_group_id INTEGER REFERENCES tender_groups(id),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','accepted','rejected')),
    reviewer        TEXT,
    reviewed_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);


-- =============================================================================
-- SECTION 4 — documents + document_diffs (the diff engine's evidence layer)
-- =============================================================================
CREATE TABLE IF NOT EXISTS documents (
    id                    INTEGER PRIMARY KEY,
    tender_id             INTEGER REFERENCES tenders(id) ON DELETE CASCADE,
    source_key            TEXT,
    doc_type              TEXT NOT NULL DEFAULT 'other' CHECK (doc_type IN
                            ('rfq','corrigendum','clarification','prebid_minutes',
                             'award','loa','other')),
    title                 TEXT,
    source_url            TEXT NOT NULL,
    content_hash          TEXT,                     -- sha256 of fetched bytes; dedup + change detection
    fetched_at            TEXT NOT NULL DEFAULT (datetime('now')),
    page_count            INTEGER,
    extraction_method     TEXT CHECK (extraction_method IN
                            ('text_layer','ocr','manual','none')),
    extraction_confidence TEXT CHECK (extraction_confidence IN ('high','low','unknown')),
    -- supersession chain: a corrigendum supersedes the doc it amends. is_current
    -- is maintained by the pipeline (set superseded docs to 0). Hard gate Day 4:
    -- a superseded doc must NEVER render as current.
    supersedes_document_id INTEGER REFERENCES documents(id),
    is_current            INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    archived_path         TEXT,                     -- local snapshot path
    confidence            TEXT NOT NULL DEFAULT 'reported'
                          CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    coverage_boundary     TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- document_diffs: every diff points at TWO real documents rows. If a diff can't
-- be traced to specific documents, it is not stored and not displayed.
CREATE TABLE IF NOT EXISTS document_diffs (
    id                INTEGER PRIMARY KEY,
    tender_id         INTEGER REFERENCES tenders(id) ON DELETE CASCADE,
    from_document_id  INTEGER NOT NULL REFERENCES documents(id),
    to_document_id    INTEGER NOT NULL REFERENCES documents(id),
    section_label     TEXT,
    classification    TEXT NOT NULL DEFAULT 'editorial' CHECK (classification IN
                        ('deadline','eligibility','technical_spec','financial_terms',
                         'scope','contact','editorial','no_change')),
    before_text       TEXT,
    after_text        TEXT,
    confidence        TEXT NOT NULL DEFAULT 'reported'
                      CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    computation_method TEXT,                        -- e.g. 'paragraph_difflib_v1'
    coverage_boundary TEXT,
    computed_at       TEXT NOT NULL DEFAULT (datetime('now'))
);


-- =============================================================================
-- SECTION 5 — claims + claim_reviews (answerability lattice + expert-minutes)
-- =============================================================================
-- A claim is an atomic assertion about a subject record. answerability_state is
-- the note's lattice. claim_reviews is THE decisive-metric instrument: every
-- editorial touch logs minutes_spent and correct_on_first_pass, so you can plot
-- expert-minutes-per-trusted-output over time and test whether it falls.
-- =============================================================================
CREATE TABLE IF NOT EXISTS claims (
    id                 INTEGER PRIMARY KEY,
    subject_table      TEXT NOT NULL,               -- 'tenders' | 'tender_events' | 'document_diffs' | ...
    subject_id         INTEGER NOT NULL,
    assertion_text     TEXT NOT NULL,
    answerability_state TEXT NOT NULL DEFAULT 'requires_review' CHECK (answerability_state IN
                         ('established','conditional','conflicting',
                          'missing_strong_coverage','missing_weak_coverage',
                          'coverage_unknown','requires_review')),
    coverage_boundary  TEXT,
    source_keys        TEXT,                         -- JSON array of source_key
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS claim_reviews (
    id                   INTEGER PRIMARY KEY,
    claim_id             INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    reviewer             TEXT,
    minutes_spent        REAL,
    correct_on_first_pass INTEGER CHECK (correct_on_first_pass IN (0,1) OR correct_on_first_pass IS NULL),
    corrected_assertion  TEXT,
    action_gate_state    TEXT CHECK (action_gate_state IN
                           ('safe_to_monitor','safe_to_read','safe_to_prepare',
                            'review_required','blocked') OR action_gate_state IS NULL),
    reviewed_at          TEXT NOT NULL DEFAULT (datetime('now'))
);


-- =============================================================================
-- SECTION 6 — dangling_references (known-unknowns queue)
-- =============================================================================
CREATE TABLE IF NOT EXISTS dangling_references (
    id                    INTEGER PRIMARY KEY,
    source_record_table   TEXT NOT NULL,
    source_record_id      INTEGER NOT NULL,
    referenced_entity     TEXT NOT NULL,
    reference_type        TEXT NOT NULL CHECK (reference_type IN
                            ('tender_ref','org_name','deployment_count',
                             'bid_result','fleet_count','charging_asset',
                             'document','other')),
    resolution_status     TEXT NOT NULL DEFAULT 'unresolved' CHECK (resolution_status IN
                            ('resolved','unresolved','conflict','needs_review','wont_fix')),
    resolved_record_table TEXT,
    resolved_record_id    INTEGER,
    conflict_notes        TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);


-- =============================================================================
-- SECTION 7 — org_aliases (entity resolution; migrate out of JSON blob)
-- =============================================================================
CREATE TABLE IF NOT EXISTS org_aliases (
    id          INTEGER PRIMARY KEY,
    org_id      INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    alias_type  TEXT CHECK (alias_type IN
                  ('trade_name','spv_name','bse_name','tender_name',
                   'press_name','vahan_name','other')),
    source_key  TEXT,
    confirmed   INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0,1)),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, alias)
);

-- Migrate existing organizations.aliases JSON arrays into rows (confirmed=1,
-- since they were curated). Guards against NULL/empty. Requires SQLite JSON1
-- (bundled in modern sqlite3). If JSON1 is unavailable, skip this statement and
-- backfill manually.
INSERT OR IGNORE INTO org_aliases (org_id, alias, alias_type, confirmed)
SELECT o.id, je.value, 'other', 1
FROM organizations o,
     json_each(o.aliases) je
WHERE o.aliases IS NOT NULL
  AND o.aliases <> ''
  AND json_valid(o.aliases) = 1;
-- organizations.aliases is left in place (deprecated) to avoid a destructive
-- table rebuild. Stop writing to it; read from org_aliases going forward.


-- =============================================================================
-- SECTION 8 — triggers
-- =============================================================================

-- tenders.status derived from tender_events. Stop setting status directly in
-- the scraper; insert a tender_events row and let this maintain status.
CREATE TRIGGER IF NOT EXISTS trg_tenders_status_from_events
AFTER INSERT ON tender_events
BEGIN
    UPDATE tenders SET
        status = CASE NEW.event_type
            WHEN 'issued'             THEN 'announced'
            WHEN 'corrigendum'        THEN tenders.status
            WHEN 'deadline_extended'  THEN 'extended'
            WHEN 'prebid_held'        THEN 'open'
            WHEN 'bids_opened'        THEN 'bids_opened'
            WHEN 'technical_results'  THEN 'bids_opened'
            WHEN 'financial_results'  THEN 'bids_opened'
            WHEN 'loa_issued'         THEN 'awarded'
            WHEN 'awarded'            THEN 'awarded'
            WHEN 'cancelled'          THEN 'cancelled'
            ELSE tenders.status
        END,
        updated_at = datetime('now')
    WHERE id = NEW.tender_id;
END;

-- source_coverage freshness from scrape_runs completion.
CREATE TRIGGER IF NOT EXISTS trg_scrape_run_update_coverage
AFTER UPDATE OF status ON scrape_runs
WHEN NEW.status IN ('ok','empty','error') AND NEW.source_key IS NOT NULL
BEGIN
    UPDATE source_coverage SET
        last_crawled_at = COALESCE(NEW.finished_at, datetime('now')),
        crawl_status    = CASE NEW.status
                            WHEN 'ok'    THEN 'ok'
                            WHEN 'empty' THEN 'ok'
                            WHEN 'error' THEN 'failed'
                          END,
        updated_at      = datetime('now')
    WHERE source_key = NEW.source_key;
END;

-- updated_at maintenance on new tables
CREATE TRIGGER IF NOT EXISTS trg_groups_updated AFTER UPDATE ON tender_groups
BEGIN UPDATE tender_groups SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_lots_updated AFTER UPDATE ON tender_lots
BEGIN UPDATE tender_lots SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_claims_updated AFTER UPDATE ON claims
BEGIN UPDATE claims SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_dangling_updated AFTER UPDATE ON dangling_references
BEGIN UPDATE dangling_references SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_source_coverage_updated AFTER UPDATE ON source_coverage
BEGIN UPDATE source_coverage SET updated_at = datetime('now') WHERE id = NEW.id; END;


-- =============================================================================
-- SECTION 9 — indexes
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_tenders_group          ON tenders(group_id);
CREATE INDEX IF NOT EXISTS idx_tenders_scheme         ON tenders(scheme);
CREATE INDEX IF NOT EXISTS idx_tenders_confidence     ON tenders(confidence);
CREATE INDEX IF NOT EXISTS idx_tender_lots_tender     ON tender_lots(tender_id);
CREATE INDEX IF NOT EXISTS idx_tender_lots_group      ON tender_lots(group_id);
CREATE INDEX IF NOT EXISTS idx_tender_events_source   ON tender_events(source_key);
CREATE INDEX IF NOT EXISTS idx_documents_tender       ON documents(tender_id, is_current);
CREATE INDEX IF NOT EXISTS idx_documents_supersedes   ON documents(supersedes_document_id);
CREATE INDEX IF NOT EXISTS idx_diffs_tender           ON document_diffs(tender_id);
CREATE INDEX IF NOT EXISTS idx_claims_subject         ON claims(subject_table, subject_id);
CREATE INDEX IF NOT EXISTS idx_claim_reviews_claim    ON claim_reviews(claim_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_pending    ON grouping_suggestions(status)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_dangling_open          ON dangling_references(resolution_status)
    WHERE resolution_status IN ('unresolved','conflict','needs_review');
CREATE INDEX IF NOT EXISTS idx_org_aliases_alias      ON org_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_source_coverage_grade  ON source_coverage(coverage_grade);


-- Safe backfill: existing events get captured_at from created_at.
UPDATE tender_events SET captured_at = created_at WHERE captured_at IS NULL;


COMMIT;
PRAGMA foreign_keys = ON;

-- =============================================================================
-- POST-MIGRATION BACKFILL (run manually, reviewed, not in this transaction)
-- =============================================================================
--  0. tender_events.captured_at: backfill existing rows from created_at:
--       UPDATE tender_events SET captured_at = created_at WHERE captured_at IS NULL;
--     (done automatically below, inside the transaction, since it is safe.)
--  1. tenders.scheme: backfill from title/ref ('PM e-Bus Sewa' -> pm_ebus_sewa,
--     'PM E-DRIVE' -> pm_edrive, etc.). Currently defaulted to 'unknown'.
--  2. tenders.source_key: set 'cesl' for existing CESL rows; 'press'/'manual'
--     for hand-curated ones.
--  3. tenders.is_multi_city: set 1 for the PM E-DRIVE multi-lot tenders.
--  4. tenders.confidence: map the site's existing high/medium/low to
--     confirmed/reported/estimated as appropriate.
--  5. Existing deployments/bids: set source_key from their source_url.
-- Do these as reviewed UPDATEs; they encode editorial judgment, so they are
-- claim_reviews-worthy (log the minutes).
-- =============================================================================
