-- ev-bus-tracker reference-data seed.
--
-- Idempotent: every statement is INSERT OR IGNORE keyed on a UNIQUE column,
-- so running this on a fresh DB populates reference data and running it on the
-- live DB (rows already present) is a no-op. Executed by get_db() after
-- schema.sql on every connection.
--
-- source_coverage: the declared source universe (the "honesty layer").
-- This block is kept BYTE-IDENTICAL to the seed block in
-- db/migrate_v1_to_v2.sql so the one-time migration and fresh builds agree.
-- If you change one, change the other.

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
