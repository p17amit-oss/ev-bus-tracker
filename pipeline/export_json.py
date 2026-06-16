"""Export the SQLite DB to JSON files the Astro site builds from.

Keeps the site build dependency-free on the Python side: Cloudflare Pages
just runs `astro build` against committed JSON. Run after scrapers, or let
the GitHub Action do it.

FACTS-ONLY CONTRACT (Option C, fact/editorial split)
----------------------------------------------------
This exporter emits ONLY machine-derivable facts from the database. It writes
the tenders facts to `tenders_facts.json` and must NEVER write `tenders.json`
or `tenders_editorial.json`. Editorial judgment (why_it_matters, key_risks,
eligibility_summary, notes, tags) lives in the hand-maintained
`tenders_editorial.json`; a separate merge step joins facts + editorial into
the `tenders.json` the site reads. Keeping the exporter blind to those files
means a scrape-and-export can never clobber human-authored prose.

The set of files this script writes is fixed and explicit:
  tenders_facts.json, organizations.json, registrations_monthly.json,
  deployments.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from common import get_db  # noqa: E402

OUT_DIR = REPO_ROOT / "site" / "src" / "data"

# Files this exporter is permitted to write. tenders.json and
# tenders_editorial.json are deliberately absent: the exporter must not touch
# either. This list is asserted against before writing as a safety rail.
ALLOWED_OUTPUTS = {
    "tenders_facts.json",
    "organizations.json",
    "registrations_monthly.json",
    "deployments.json",
    "coverage.json",
}

# Pinned slugs for tenders whose URLs are already public and must stay stable.
# These slugs are irregular composites (scheme + tender number + bus count)
# that no generic algorithm reproduces, so we pin them by tender_ref rather
# than risk a URL change. New tenders not listed here fall back to slugify().
STABLE_SLUGS = {
    "CESL/06/2026-27/PM-eBus Sewa3/262704003": "cesl-pm-ebus-sewa-3-3604",
    "CESL/06/2025-26/PM E-Drive/252601015": "cesl-pm-edrive-2-2900",
}

# DB confidence controlled vocabulary -> site display label. The numeric
# confidence_score is intentionally NOT emitted; the DB has no such column and
# the label is the only confidence signal the site renders.
CONFIDENCE_LABEL = {
    "confirmed": "high",
    "reported": "medium",
    "estimated": "low",
    "inferred": "low",
}

# source_coverage.source_type -> the value the site renders. The tender page
# only does source_type.replace('_', ' ') for display, so the mapping exists
# purely for parity with the existing hand-authored content ('government
# portal'). Unmapped types pass through unchanged.
SOURCE_TYPE_MAP = {
    "portal": "government_portal",
}

# Factual tender columns emitted to the site. Explicit list, never SELECT *,
# so a future ALTER TABLE cannot silently leak a column into the public JSON.
TENDER_FACT_COLUMNS = [
    "id", "tender_ref", "title", "bus_count", "bus_length_m", "ac_type",
    "states", "cities", "estimated_value_cr", "contract_years", "issue_date",
    "prebid_date", "bid_due_date", "status", "procurement_model", "scheme",
    "source_url", "source_key", "is_multi_city", "confidence", "group_id",
    "issuing_org_id", "lot_label", "charging_scope", "depot_scope",
]


def rows(conn, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def slugify(text: str) -> str:
    """Generic URL slug: lowercase, non-alphanumeric runs -> single hyphen.

    Fallback only — known public slugs are pinned in STABLE_SLUGS so their
    URLs never shift. Used for tenders added after this split.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "tender"


def slug_for(tender_ref: str, title: str, bus_count: int | None) -> str:
    """Stable slug for a tender. Pinned value wins; otherwise derive one."""
    if tender_ref in STABLE_SLUGS:
        return STABLE_SLUGS[tender_ref]
    base = slugify(title or tender_ref or "")
    if bus_count and str(bus_count) not in base:
        base = f"{base}-{bus_count}"
    return base


def status_history_for(conn, tender_id: int) -> list[dict]:
    """Derive the event timeline from tender_events — never hand-authored.

    Ordered by event_date then id so rows with a blank event_date stay in a
    deterministic insertion order rather than shuffling between exports.
    """
    return rows(conn, """
        SELECT event_date, event_type, details, source_url, source_key
        FROM tender_events
        WHERE tender_id = ?
        ORDER BY COALESCE(NULLIF(event_date, ''), '9999'), id
    """, (tender_id,))


def amendments_for(conn, tender_id: int) -> list[dict]:
    """Derive amendments from document_diffs, joined to the target document.

    Empty for now (no diffs captured yet), but wired so corrigenda surface
    automatically once the diff engine populates document_diffs/documents.
    """
    return rows(conn, """
        SELECT dd.id, dd.section_label, dd.classification,
               dd.before_text, dd.after_text, dd.computed_at,
               d.title      AS document_title,
               d.source_url AS document_url,
               d.doc_type   AS document_type
        FROM document_diffs dd
        LEFT JOIN documents d ON d.id = dd.to_document_id
        WHERE dd.tender_id = ?
        ORDER BY COALESCE(dd.computed_at, ''), dd.id
    """, (tender_id,))


def lots_for(conn, tender_id: int) -> list[dict]:
    """City-lot decomposition for a (usually multi-city) tender.

    Distinct from the tender-level singular scope label (tenders.lot_label,
    emitted as 'lot_label'): this is the per-city breakdown from tender_lots,
    attached as the plural 'lots' array. Tenders with no rows get []. Explicit
    column list (never SELECT *) so a future ALTER cannot leak a column.
    Ordered by bus_count DESC then city for a deterministic export.
    """
    return rows(conn, """
        SELECT lot_label, city, state, scheme, bus_count, bus_length_m,
               confidence, coverage_boundary, group_id
        FROM tender_lots
        WHERE tender_id = ?
        ORDER BY bus_count DESC, city
    """, (tender_id,))


def tender_facts(conn) -> list[dict]:
    """Facts-only tender export: explicit columns + derived timeline/labels.

    Source metadata (source_name, source_type) is derived from the
    source_coverage registry via source_key, so the registry stays the single
    home for source descriptions. last_checked_at is derived from the latest
    captured event, falling back to the tender's own updated_at when it has no
    events yet — so it can never go stale relative to what we actually saw.
    """
    col_list = ", ".join(f"t.{c}" for c in TENDER_FACT_COLUMNS)
    base = rows(conn, f"""
        SELECT {col_list},
               o.name AS issuer_name,
               o.slug AS issuer_slug,
               sc.source_name AS source_name,
               sc.source_type AS source_type_raw,
               substr(COALESCE(
                   (SELECT MAX(te.captured_at) FROM tender_events te
                    WHERE te.tender_id = t.id AND te.captured_at IS NOT NULL
                          AND te.captured_at <> ''),
                   t.updated_at
               ), 1, 10) AS last_checked_at
        FROM tenders t
        LEFT JOIN organizations o   ON o.id = t.issuing_org_id
        LEFT JOIN source_coverage sc ON sc.source_key = t.source_key
        ORDER BY t.created_at DESC
    """)
    for t in base:
        t["slug"] = slug_for(t["tender_ref"], t["title"], t.get("bus_count"))
        t["confidence_label"] = CONFIDENCE_LABEL.get(t.get("confidence"), "low")
        raw_type = t.pop("source_type_raw")
        t["source_type"] = SOURCE_TYPE_MAP.get(raw_type, raw_type)
        t["status_history"] = status_history_for(conn, t["id"])
        t["amendments"] = amendments_for(conn, t["id"])
        t["lots"] = lots_for(conn, t["id"])
    return base


def main() -> None:
    conn = get_db()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    exports = {
        "tenders_facts.json": tender_facts(conn),
        "organizations.json": rows(conn, "SELECT * FROM organizations ORDER BY name"),
        "registrations_monthly.json": rows(conn, """
            SELECT month, maker_name_raw AS maker, SUM(count) AS count
            FROM registrations GROUP BY month, maker_name_raw
            ORDER BY month, count DESC"""),
        "deployments.json": rows(conn, """
            SELECT d.*, op.name AS operator_name, oe.name AS oem_name
            FROM deployments d
            LEFT JOIN organizations op ON op.id = d.operator_org_id
            LEFT JOIN organizations oe ON oe.id = d.oem_org_id
            ORDER BY d.deployment_date DESC"""),
        # Source-coverage registry — standalone reference data the methodology
        # page renders directly, so its active/planned split can never drift
        # from the DB. Active sources (automated/manual) sort before planned;
        # ties break on source_key.
        "coverage.json": rows(conn, """
            SELECT source_key, source_name, source_type, coverage_grade,
                   ingest_mode, crawl_status, last_crawled_at, known_gaps
            FROM source_coverage
            ORDER BY CASE ingest_mode
                       WHEN 'automated' THEN 0
                       WHEN 'manual'    THEN 0
                       WHEN 'planned'   THEN 1
                       ELSE 2
                     END,
                     source_key"""),
    }

    # Safety rail: refuse to write anything outside the allowed set, so this
    # exporter can never overwrite tenders.json or tenders_editorial.json.
    illegal = set(exports) - ALLOWED_OUTPUTS
    if illegal:
        raise SystemExit(f"export_json.py refused to write disallowed files: {illegal}")

    for filename, data in exports.items():
        (OUT_DIR / filename).write_text(
            json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        print(f"wrote {filename}: {len(data)} rows")


if __name__ == "__main__":
    main()
