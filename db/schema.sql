-- ev-bus-tracker SQLite schema
-- Conventions:
--   * All dates are ISO-8601 TEXT (YYYY-MM-DD); months are YYYY-MM.
--   * Money is in INR crore (REAL) unless the column name says otherwise.
--   * Every scraped row keeps source_url + raw payload so facts are auditable.
--   * updated_at maintained by triggers; do not set it manually.

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
    aliases         TEXT,                            -- JSON array of alternate names
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- tenders: one row per tender / RFP (CESL, state STUs, smart-city SPVs)
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
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- tender_events: the timeline of each tender (corrigenda, extensions, awards)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tender_events (
    id          INTEGER PRIMARY KEY,
    tender_id   INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL CHECK (event_type IN
                  ('issued','corrigendum','prebid_held','deadline_extended',
                   'bids_opened','technical_results','financial_results',
                   'awarded','loa_issued','cancelled','other')),
    event_date  TEXT,
    details     TEXT,
    source_url  TEXT,
    dedupe_key  TEXT UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- bids: who bid what on which tender (per lot where applicable)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bids (
    id                INTEGER PRIMARY KEY,
    tender_id         INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    bidder_org_id     INTEGER REFERENCES organizations(id),
    bidder_name_raw   TEXT,                          -- as printed, pre-entity-match
    lot               TEXT,                          -- lot/cluster identifier
    bus_count         INTEGER,
    price_per_km_inr  REAL,                          -- GCC tenders quote Rs/km
    bid_amount_cr     REAL,                          -- outright tenders quote total
    rank              INTEGER,                       -- L1 = 1
    is_winner         INTEGER NOT NULL DEFAULT 0 CHECK (is_winner IN (0,1)),
    notes             TEXT,
    source_url        TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- deployments: buses actually on the road (city x operator x OEM)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deployments (
    id               INTEGER PRIMARY KEY,
    operator_org_id  INTEGER REFERENCES organizations(id),
    oem_org_id       INTEGER REFERENCES organizations(id),
    tender_id        INTEGER REFERENCES tenders(id), -- provenance when known
    city             TEXT,
    state            TEXT,
    bus_count        INTEGER,
    bus_model        TEXT,                           -- e.g. 'Olectra K9', 'Switch EiV12'
    depot            TEXT,
    deployment_date  TEXT,                           -- first revenue service
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                       ('announced','delivered','active','retired','unknown')),
    source_url       TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
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
    UNIQUE (month, state, rto, maker_name_raw, vehicle_class, fuel)
);

-- ---------------------------------------------------------------------------
-- charging_events: depot commissioning, charger orders, grid upgrades
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS charging_events (
    id            INTEGER PRIMARY KEY,
    event_type    TEXT NOT NULL CHECK (event_type IN
                    ('depot_commissioned','charger_order','charger_installed',
                     'grid_upgrade','partnership','other')),
    org_id        INTEGER REFERENCES organizations(id),
    city          TEXT,
    state         TEXT,
    charger_count INTEGER,
    capacity_kw   REAL,                              -- aggregate capacity if stated
    event_date    TEXT,
    details       TEXT,
    source_url    TEXT,
    dedupe_key    TEXT UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- announcements: staging for raw corporate disclosures (BSE) before triage.
-- The BSE scraper writes here; classification into tenders/deployments is a
-- second pass so a parsing bug never corrupts the curated tables.
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
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- scrape_runs: one row per scraper execution; powers the health-check digest
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_runs (
    id             INTEGER PRIMARY KEY,
    scraper        TEXT NOT NULL,                    -- 'bse' | 'cesl' | 'vahan'
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running' CHECK (status IN
                     ('running','ok','empty','error')),
    rows_found     INTEGER NOT NULL DEFAULT 0,       -- rows seen at the source
    rows_inserted  INTEGER NOT NULL DEFAULT 0,       -- net-new rows written
    error          TEXT
);

-- Indexes for the query patterns the site and digest actually use.
CREATE INDEX IF NOT EXISTS idx_tenders_status        ON tenders(status);
CREATE INDEX IF NOT EXISTS idx_tenders_bid_due       ON tenders(bid_due_date);
CREATE INDEX IF NOT EXISTS idx_tender_events_tender  ON tender_events(tender_id, event_date);
CREATE INDEX IF NOT EXISTS idx_bids_tender           ON bids(tender_id);
CREATE INDEX IF NOT EXISTS idx_deployments_city      ON deployments(state, city);
CREATE INDEX IF NOT EXISTS idx_registrations_month   ON registrations(month, state);
CREATE INDEX IF NOT EXISTS idx_announcements_triage  ON announcements(triaged, announced_at);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_scraper   ON scrape_runs(scraper, started_at);

-- updated_at triggers
CREATE TRIGGER IF NOT EXISTS trg_orgs_updated AFTER UPDATE ON organizations
BEGIN UPDATE organizations SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_tenders_updated AFTER UPDATE ON tenders
BEGIN UPDATE tenders SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_deployments_updated AFTER UPDATE ON deployments
BEGIN UPDATE deployments SET updated_at = datetime('now') WHERE id = NEW.id; END;
