-- ev-bus-tracker SQLite schema (v2 — reconciled with migrate_v1_to_v2.sql + scope columns)
-- Conventions:
--   * All dates are ISO-8601 TEXT (YYYY-MM-DD); months are YYYY-MM.
--   * Money is in INR crore (REAL) unless the column name says otherwise.
--   * Every scraped row keeps source_url + raw payload so facts are auditable.
--   * updated_at maintained by triggers; do not set it manually.
--
-- IDEMPOTENT BY CONTRACT: this file runs on every get_db() connection against
-- the LIVE database. Every statement is CREATE ... IF NOT EXISTS, so re-running
-- is a no-op. It contains NO ALTER TABLE and NO data backfill/seed INSERTs —
-- those live ONLY in db/migrate_v1_to_v2.sql (the one-time migration). On a
-- fresh DB this file creates the full v2 shape directly (all columns inline);
-- on an existing table CREATE TABLE IF NOT EXISTS is skipped and the inline
-- column list is ignored — so it can never conflict with the migrated DB.
-- Epistemic-metadata semantics (reused across tables):
--   confidence:      confirmed | reported | estimated | inferred
--   derivation_type: direct | computed | cross_referenced | inferred
--   verified_by:     NULL (unreviewed) | cross_source | human
--   source_key:      FK-by-convention to source_coverage.source_key

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- organizations: OEMs, STUs/STAs, agencies (CESL, NITI), operators, financiers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organizations (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,            -- url-safe, used for SEO pages
    org_type        TEXT NOT NULL CHECK (org_type IN
                      ('oem','operator','transit_authority','agency',
                       'charging_provider','financier','other')),
    state           TEXT,                            -- home state, NULL for national
    city            TEXT,
    website         TEXT,
    bse_scrip_code  TEXT,                            -- e.g. '532439' for Olectra
    nse_symbol      TEXT,
    parent_org_id   INTEGER REFERENCES organizations(id),
    aliases         TEXT,                            -- DEPRECATED JSON array; read from org_aliases
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- tender_groups: identity of one real-world procurement; members cluster via
-- group_id and are never collapsed. Defined before tenders (tenders.group_id
-- references it) so a fresh build has no forward FK reference.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tender_groups (
    id                         INTEGER PRIMARY KEY,
    canonical_label            TEXT NOT NULL,        -- e.g. 'PM E-DRIVE Delhi 3,330-bus GCC'
    scheme                     TEXT NOT NULL DEFAULT 'unknown',
    primary_city               TEXT,
    representative_member_type TEXT CHECK (representative_member_type IN ('tender','lot')),
    representative_member_id   INTEGER,             -- id in tenders or tender_lots
    representative_bus_count   INTEGER,             -- count used for aggregation
    confidence                 TEXT NOT NULL DEFAULT 'reported'
                               CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    verified_by                TEXT
                               CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL),
    created_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                 TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- tenders: one row per tender / RFP (CESL, state STUs, smart-city SPVs).
-- Each row is ONE source-observation; group_id clusters observations of the
-- same procurement.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenders (
    id                  INTEGER PRIMARY KEY,
    tender_ref          TEXT,                        -- issuer's tender number
    title               TEXT NOT NULL,
    issuing_org_id      INTEGER REFERENCES organizations(id),
    procurement_model   TEXT CHECK (procurement_model IN
                          ('gcc','outright','lease','hybrid','unknown')),
    bus_count           INTEGER,
    bus_length_m        TEXT,                        -- '9m', '12m', '9m+12m', etc.
    ac_type             TEXT CHECK (ac_type IN ('ac','non_ac','mixed','unknown')),
    states              TEXT,                        -- JSON array, multi-state tenders
    cities              TEXT,                        -- JSON array
    estimated_value_cr  REAL,
    contract_years      INTEGER,                     -- GCC concession period
    issue_date          TEXT,
    prebid_date         TEXT,
    bid_due_date        TEXT,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                          ('announced','open','extended','bids_opened',
                           'awarded','cancelled','unknown')),
    source_url          TEXT,
    raw_text            TEXT,                        -- raw listing text for audit
    dedupe_key          TEXT UNIQUE,                 -- normalized ref+issuer hash
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    -- v2 epistemic metadata
    source_key          TEXT,
    confidence          TEXT NOT NULL DEFAULT 'reported'
                          CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    derivation_type     TEXT NOT NULL DEFAULT 'direct'
                          CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred')),
    coverage_boundary   TEXT,
    verified_by         TEXT
                          CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL),
    -- v2 grouping
    group_id            INTEGER REFERENCES tender_groups(id),
    is_multi_city       INTEGER NOT NULL DEFAULT 0 CHECK (is_multi_city IN (0,1)),
    scheme              TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (scheme IN ('pm_ebus_sewa','pm_edrive','fame_2',
                                            'state_funded','smart_city','other','unknown')),
    -- v2 scope columns
    lot_label           TEXT,
    charging_scope      TEXT CHECK (charging_scope IN ('included','excluded','partial','unknown')),
    depot_scope         TEXT CHECK (depot_scope IN ('included','excluded','partial','unknown'))
);

-- ---------------------------------------------------------------------------
-- tender_events: the timeline of each tender (corrigenda, extensions, awards).
-- Append-only; the status-from-events trigger derives tenders.status.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tender_events (
    id                INTEGER PRIMARY KEY,
    tender_id         INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    event_type        TEXT NOT NULL CHECK (event_type IN
                        ('issued','corrigendum','prebid_held','deadline_extended',
                         'bids_opened','technical_results','financial_results',
                         'awarded','loa_issued','cancelled','other')),
    event_date        TEXT,
    details           TEXT,
    source_url        TEXT,
    dedupe_key        TEXT UNIQUE,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    -- v2: per-event provenance. captured_at set by the application on insert.
    source_key        TEXT,
    captured_at       TEXT,
    archived_path     TEXT,
    confidence        TEXT NOT NULL DEFAULT 'reported'
                        CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    derivation_type   TEXT NOT NULL DEFAULT 'direct'
                        CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred')),
    coverage_boundary TEXT,
    verified_by       TEXT
                        CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL)
);

-- ---------------------------------------------------------------------------
-- tender_lots: decomposition of a (usually multi-city) tender into city lots.
-- Single-city tenders need no lot row; they group at tenders.group_id level.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tender_lots (
    id                INTEGER PRIMARY KEY,
    tender_id         INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    lot_label         TEXT,                          -- issuer's lot/package label
    city              TEXT,
    state             TEXT,
    scheme            TEXT NOT NULL DEFAULT 'unknown',
    bus_count         INTEGER,
    bus_length_m      TEXT,
    group_id          INTEGER REFERENCES tender_groups(id),
    confidence        TEXT NOT NULL DEFAULT 'reported'
                        CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    derivation_type   TEXT NOT NULL DEFAULT 'direct'
                        CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred')),
    coverage_boundary TEXT,
    verified_by       TEXT CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- bids: who bid what on which tender (per lot where applicable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bids (
    id                 INTEGER PRIMARY KEY,
    tender_id          INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    bidder_org_id      INTEGER REFERENCES organizations(id),
    bidder_name_raw    TEXT,                         -- as printed, pre-entity-match
    lot                TEXT,                         -- lot/cluster identifier
    bus_count          INTEGER,
    price_per_km_inr   REAL,                         -- GCC tenders quote Rs/km
    bid_amount_cr      REAL,                         -- outright tenders quote total
    rank               INTEGER,                      -- L1 = 1
    is_winner          INTEGER NOT NULL DEFAULT 0 CHECK (is_winner IN (0,1)),
    notes              TEXT,
    source_url         TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    -- v2
    source_key         TEXT,
    consortium_members TEXT,                         -- JSON array of org ids
    price_basis        TEXT,                         -- with_electricity | ex_electricity | unspecified
    escalation_terms   TEXT,
    confidence         TEXT NOT NULL DEFAULT 'reported'
                         CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    derivation_type    TEXT NOT NULL DEFAULT 'direct'
                         CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred')),
    coverage_boundary  TEXT,
    verified_by        TEXT
                         CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL)
);

-- ---------------------------------------------------------------------------
-- deployments: buses actually on the road (city x operator x OEM)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deployments (
    id                INTEGER PRIMARY KEY,
    operator_org_id   INTEGER REFERENCES organizations(id),
    oem_org_id        INTEGER REFERENCES organizations(id),
    tender_id         INTEGER REFERENCES tenders(id), -- provenance when known
    city              TEXT,
    state             TEXT,
    bus_count         INTEGER,
    bus_model         TEXT,                           -- e.g. 'Olectra K9', 'Switch EiV12'
    depot             TEXT,
    deployment_date   TEXT,                           -- first revenue service
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                        ('announced','delivered','active','retired','unknown')),
    source_url        TEXT,
    notes             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    -- v2
    source_key        TEXT,
    confidence        TEXT NOT NULL DEFAULT 'reported'
                        CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    derivation_type   TEXT NOT NULL DEFAULT 'direct'
                        CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred')),
    coverage_boundary TEXT,
    verified_by       TEXT
                        CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL)
);

-- ---------------------------------------------------------------------------
-- registrations: Vahan monthly EV bus registrations (state x maker x month)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS registrations (
    id              INTEGER PRIMARY KEY,
    month           TEXT NOT NULL,                   -- 'YYYY-MM'
    state           TEXT NOT NULL,
    rto             TEXT,                            -- NULL = state-level rollup
    maker_org_id    INTEGER REFERENCES organizations(id),
    maker_name_raw  TEXT NOT NULL,                   -- exactly as Vahan prints it
    vehicle_class   TEXT NOT NULL DEFAULT 'BUS',
    fuel            TEXT NOT NULL DEFAULT 'PURE EV',
    count           INTEGER NOT NULL CHECK (count >= 0),
    captured_at     TEXT NOT NULL DEFAULT (datetime('now')),
    source_key      TEXT NOT NULL DEFAULT 'vahan',   -- v2
    UNIQUE (month, state, rto, maker_name_raw, vehicle_class, fuel)
);

-- ---------------------------------------------------------------------------
-- charging_events: depot commissioning, charger orders, grid upgrades
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS charging_events (
    id                INTEGER PRIMARY KEY,
    event_type        TEXT NOT NULL CHECK (event_type IN
                        ('depot_commissioned','charger_order','charger_installed',
                         'grid_upgrade','partnership','other')),
    org_id            INTEGER REFERENCES organizations(id),
    city              TEXT,
    state             TEXT,
    charger_count     INTEGER,
    capacity_kw       REAL,                          -- aggregate capacity if stated
    event_date        TEXT,
    details           TEXT,
    source_url        TEXT,
    dedupe_key        TEXT UNIQUE,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    -- v2
    source_key        TEXT,
    confidence        TEXT NOT NULL DEFAULT 'reported'
                        CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    derivation_type   TEXT NOT NULL DEFAULT 'direct'
                        CHECK (derivation_type IN ('direct','computed','cross_referenced','inferred')),
    coverage_boundary TEXT,
    verified_by       TEXT
                        CHECK (verified_by IN ('cross_source','human') OR verified_by IS NULL)
);

-- ---------------------------------------------------------------------------
-- announcements: staging for raw corporate disclosures (BSE) before triage.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS announcements (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL DEFAULT 'bse',
    org_id        INTEGER REFERENCES organizations(id),
    scrip_code    TEXT,
    headline      TEXT NOT NULL,
    category      TEXT,                              -- BSE's own category field
    announced_at  TEXT,                              -- exchange dissemination time
    pdf_url       TEXT,
    matched_terms TEXT,                              -- JSON array of hit keywords
    triaged       INTEGER NOT NULL DEFAULT 0 CHECK (triaged IN (0,1)),
    dedupe_key    TEXT NOT NULL UNIQUE,              -- BSE news id
    raw_json      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- v2
    source_key    TEXT NOT NULL DEFAULT 'bse',
    triage_result TEXT CHECK (triage_result IN
                    ('order_confirmed','deployment_update','capex_signal',
                     'policy_commentary','tender','charging','boilerplate','unclear')
                    OR triage_result IS NULL)
);

-- ---------------------------------------------------------------------------
-- scrape_runs: one row per scraper execution; powers the health-check digest
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_runs (
    id                  INTEGER PRIMARY KEY,
    scraper             TEXT NOT NULL,               -- 'bse' | 'cesl' | 'vahan'
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running' CHECK (status IN
                          ('running','ok','empty','error')),
    rows_found          INTEGER NOT NULL DEFAULT 0,  -- rows seen at the source
    rows_inserted       INTEGER NOT NULL DEFAULT 0,  -- net-new rows written
    error               TEXT,
    -- v2: richer run accounting for the health digest
    source_key          TEXT,
    rows_updated        INTEGER NOT NULL DEFAULT 0,
    dangling_created    INTEGER NOT NULL DEFAULT 0,
    suggestions_created INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- source_coverage: the honesty layer — declared, graded source universe.
-- Seed rows live in db/seed.sql, NOT here (idempotency).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_coverage (
    id                  INTEGER PRIMARY KEY,
    source_key          TEXT NOT NULL UNIQUE,
    source_name         TEXT NOT NULL,
    source_type         TEXT CHECK (source_type IN
                          ('portal','exchange','registry','aggregator',
                           'press','govt_notification','parliamentary','user')),
    coverage_grade      TEXT NOT NULL DEFAULT 'C' CHECK (coverage_grade IN ('A','B','C')),
    ingest_mode         TEXT NOT NULL DEFAULT 'planned'
                          CHECK (ingest_mode IN ('automated','manual','planned')),
    coverage_start_date TEXT,
    last_crawled_at     TEXT,
    crawl_status        TEXT NOT NULL DEFAULT 'never'
                          CHECK (crawl_status IN ('ok','partial','failed','blocked','never')),
    known_gaps          TEXT,
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- grouping_suggestions: non-destructive review queue for ambiguous matches
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS grouping_suggestions (
    id                 INTEGER PRIMARY KEY,
    member_a_type      TEXT NOT NULL CHECK (member_a_type IN ('tender','lot')),
    member_a_id        INTEGER NOT NULL,
    member_b_type      TEXT NOT NULL CHECK (member_b_type IN ('tender','lot')),
    member_b_id        INTEGER NOT NULL,
    signals_matched    TEXT,                         -- JSON: which of the 4 matched
    match_score        REAL,                         -- 0..1, advisory
    suggested_group_id INTEGER REFERENCES tender_groups(id),
    status             TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','accepted','rejected')),
    reviewer           TEXT,
    reviewed_at        TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- documents: versioned tender docs with supersession (the diff evidence layer)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id                     INTEGER PRIMARY KEY,
    tender_id              INTEGER REFERENCES tenders(id) ON DELETE CASCADE,
    source_key             TEXT,
    doc_type               TEXT NOT NULL DEFAULT 'other' CHECK (doc_type IN
                             ('rfq','corrigendum','clarification','prebid_minutes',
                              'award','loa','other')),
    title                  TEXT,
    source_url             TEXT NOT NULL,
    content_hash           TEXT,                     -- sha256 of fetched bytes
    fetched_at             TEXT NOT NULL DEFAULT (datetime('now')),
    page_count             INTEGER,
    extraction_method      TEXT CHECK (extraction_method IN ('text_layer','ocr','manual','none')),
    extraction_confidence  TEXT CHECK (extraction_confidence IN ('high','low','unknown')),
    supersedes_document_id INTEGER REFERENCES documents(id),
    is_current             INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    archived_path          TEXT,                     -- local snapshot path
    confidence             TEXT NOT NULL DEFAULT 'reported'
                             CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    coverage_boundary      TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- document_diffs: every diff points at TWO real documents rows
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_diffs (
    id                 INTEGER PRIMARY KEY,
    tender_id          INTEGER REFERENCES tenders(id) ON DELETE CASCADE,
    from_document_id   INTEGER NOT NULL REFERENCES documents(id),
    to_document_id     INTEGER NOT NULL REFERENCES documents(id),
    section_label      TEXT,
    classification     TEXT NOT NULL DEFAULT 'editorial' CHECK (classification IN
                         ('deadline','eligibility','technical_spec','financial_terms',
                          'scope','contact','editorial','no_change')),
    before_text        TEXT,
    after_text         TEXT,
    confidence         TEXT NOT NULL DEFAULT 'reported'
                         CHECK (confidence IN ('confirmed','reported','estimated','inferred')),
    computation_method TEXT,                         -- e.g. 'paragraph_difflib_v1'
    coverage_boundary  TEXT,
    computed_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- claims: atomic assertions about a subject record (the answerability lattice)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims (
    id                  INTEGER PRIMARY KEY,
    subject_table       TEXT NOT NULL,               -- 'tenders' | 'tender_events' | ...
    subject_id          INTEGER NOT NULL,
    assertion_text      TEXT NOT NULL,
    answerability_state TEXT NOT NULL DEFAULT 'requires_review' CHECK (answerability_state IN
                          ('established','conditional','conflicting',
                           'missing_strong_coverage','missing_weak_coverage',
                           'coverage_unknown','requires_review')),
    coverage_boundary   TEXT,
    source_keys         TEXT,                        -- JSON array of source_key
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- claim_reviews: the expert-minutes instrument (decisive metric)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claim_reviews (
    id                    INTEGER PRIMARY KEY,
    claim_id              INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    reviewer              TEXT,
    minutes_spent         REAL,
    correct_on_first_pass INTEGER CHECK (correct_on_first_pass IN (0,1) OR correct_on_first_pass IS NULL),
    corrected_assertion   TEXT,
    action_gate_state     TEXT CHECK (action_gate_state IN
                            ('safe_to_monitor','safe_to_read','safe_to_prepare',
                             'review_required','blocked') OR action_gate_state IS NULL),
    reviewed_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- dangling_references: the known-unknowns queue (reference-closure)
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- org_aliases: entity resolution (migrated out of organizations.aliases JSON)
-- ---------------------------------------------------------------------------
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

-- ===========================================================================
-- Indexes (v1 + v2)
-- ===========================================================================
CREATE INDEX IF NOT EXISTS idx_tenders_status        ON tenders(status);
CREATE INDEX IF NOT EXISTS idx_tenders_bid_due       ON tenders(bid_due_date);
CREATE INDEX IF NOT EXISTS idx_tenders_group         ON tenders(group_id);
CREATE INDEX IF NOT EXISTS idx_tenders_scheme        ON tenders(scheme);
CREATE INDEX IF NOT EXISTS idx_tenders_confidence    ON tenders(confidence);
CREATE INDEX IF NOT EXISTS idx_tender_events_tender  ON tender_events(tender_id, event_date);
CREATE INDEX IF NOT EXISTS idx_tender_events_source  ON tender_events(source_key);
CREATE INDEX IF NOT EXISTS idx_tender_lots_tender    ON tender_lots(tender_id);
CREATE INDEX IF NOT EXISTS idx_tender_lots_group     ON tender_lots(group_id);
CREATE INDEX IF NOT EXISTS idx_bids_tender           ON bids(tender_id);
CREATE INDEX IF NOT EXISTS idx_deployments_city      ON deployments(state, city);
CREATE INDEX IF NOT EXISTS idx_registrations_month   ON registrations(month, state);
CREATE INDEX IF NOT EXISTS idx_announcements_triage  ON announcements(triaged, announced_at);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_scraper   ON scrape_runs(scraper, started_at);
CREATE INDEX IF NOT EXISTS idx_documents_tender      ON documents(tender_id, is_current);
CREATE INDEX IF NOT EXISTS idx_documents_supersedes  ON documents(supersedes_document_id);
CREATE INDEX IF NOT EXISTS idx_diffs_tender          ON document_diffs(tender_id);
CREATE INDEX IF NOT EXISTS idx_claims_subject        ON claims(subject_table, subject_id);
CREATE INDEX IF NOT EXISTS idx_claim_reviews_claim   ON claim_reviews(claim_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_pending   ON grouping_suggestions(status)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_dangling_open         ON dangling_references(resolution_status)
    WHERE resolution_status IN ('unresolved','conflict','needs_review');
CREATE INDEX IF NOT EXISTS idx_org_aliases_alias     ON org_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_source_coverage_grade ON source_coverage(coverage_grade);

-- ===========================================================================
-- Triggers (v1 + v2)
-- ===========================================================================

-- updated_at maintenance
CREATE TRIGGER IF NOT EXISTS trg_orgs_updated AFTER UPDATE ON organizations
BEGIN UPDATE organizations SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_tenders_updated AFTER UPDATE ON tenders
BEGIN UPDATE tenders SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_deployments_updated AFTER UPDATE ON deployments
BEGIN UPDATE deployments SET updated_at = datetime('now') WHERE id = NEW.id; END;

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

-- tenders.status derived from tender_events (stop setting status directly).
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
