"""Offline fixture test for scrapers/dtc.py (Latest-Tenders feed only).

Runs against the REAL captured Delhi NIC Latest-Tenders feed HTML
(tests/fixtures/dtc_latest_tenders.html, saved verbatim from a live read-only
fetch) and an IN-MEMORY SQLite DB — never touches data/evbus.db, never hits the
network.

Run directly:  python3 tests/test_dtc.py
Or via pytest:  pytest tests/test_dtc.py

Assertions:
  1. The feed-scoped parser yields the real row count from the saved feed
     (header + chrome rows skipped, nested data table read once).
  2. The three real rows sharing ref 'NIT No 7 (2026-27) EE (C)-37' produce
     THREE DISTINCT dedupe_keys — proves ref+title disambiguation on real data.
  3. A re-list of a real row with only whitespace/case/serial-prefix changes
     produces the SAME dedupe_key — proves normalization guards over-dedup.
  4. The real window has zero bus rows; the bus keyword gate drops all of them.
  5. With a bus row whose text names no issuer (derived from the real markup):
     it is INGESTED with issuing_org_id NULL (not presumed DTC) and an
     'org_name'/'unresolved' dangling_references row is logged; source_key='dtc'
     on the tender and its 'issued' event; a second identical run is idempotent.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

import dtc  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA = (REPO_ROOT / "db" / "schema.sql").read_text()
REAL_HTML = (FIXTURES / "dtc_latest_tenders.html").read_text()

SHARED_NIT_REF = "NIT No 7 (2026-27) EE (C)-37"  # 3 distinct tenders share this ref
NO_ISSUER_BUS_REF = "NIT-99/2026-27/EECD-V/IFCD"  # synthetic bus ref naming no body


def _mem_db() -> sqlite3.Connection:
    """In-memory DB with the full schema applied. Never touches data/evbus.db."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        raise AssertionError(msg)


def _bus_variant_html() -> str:
    """Derive a bus-row HTML from the REAL fixture markup (no hand-written table).

    Rewrites the first real data row's title to a bus title naming no issuer and
    its Reference No to a non-issuer ref, leaving the real nested structure
    intact. The other real rows stay non-bus and are filtered out.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(REAL_HTML, "html.parser")
    # First data row = first tr that _row_cells accepts.
    container = dtc._feed_container(soup)
    first = next(tr for tr in container.find_all("tr") if dtc._row_cells(tr))
    tds = first.find_all("td", recursive=False)
    tds[0].clear()
    tds[0].append("1. Procurement and operation of 100 electric buses on GCC basis")
    tds[1].clear()
    tds[1].append(NO_ISSUER_BUS_REF)
    return str(soup)


def test_parser_and_dedupe_on_real_data():
    print("1-4. feed-scoped parser / ref+title dedupe / normalization / bus filter (REAL feed)")
    rows = dtc.parse_listing(REAL_HTML)
    _check(len(rows) == 10, f"parser yields the real 10 data rows (header/chrome skipped) — got {len(rows)}")

    # The three rows sharing the NIT ref must yield three DISTINCT keys.
    shared = [r for r in rows if r["ref"] == SHARED_NIT_REF]
    _check(len(shared) == 3, f"3 real rows share ref {SHARED_NIT_REF!r} — got {len(shared)}")
    keys = {dtc.make_dedupe(r["ref"], r["title"]) for r in shared}
    _check(len(keys) == 3,
           f"those 3 shared-ref rows produce 3 DISTINCT dedupe_keys (ref+title) — got {len(keys)}")

    # Normalization: same row, only whitespace/case/serial-prefix changed -> same key.
    r0 = rows[0]
    base_key = dtc.make_dedupe(r0["ref"], r0["title"])
    churned_ref = f"  {r0['ref'].upper()}   "                       # case + whitespace
    churned_title = f"  99.   {r0['title'].upper()}  "             # serial + case + whitespace
    churned_key = dtc.make_dedupe(churned_ref, churned_title)
    _check(base_key == churned_key,
           "re-list with only whitespace/case/serial-prefix change -> SAME dedupe_key")

    # Real window has zero bus rows; the gate drops all.
    bus = [r for r in rows if dtc.BUS_TERMS.search(r["raw"])]
    _check(len(bus) == 0, f"real window has 0 bus rows; keyword gate drops all — got {len(bus)}")


def test_bus_row_ingest_unresolved_issuer():
    print("5. bus-row ingest / unresolved-issuer dangling / source_key / idempotency")
    conn = _mem_db()
    html = _bus_variant_html()

    report = dtc.process(conn, dry_run=False, tenders_html=html)

    trows = conn.execute(
        "SELECT id, tender_ref, source_key, issuing_org_id FROM tenders"
    ).fetchall()
    _check(len(trows) == 1, f"exactly 1 bus tender ingested (others filtered) — got {len(trows)}")
    t = trows[0]
    _check(t["tender_ref"] == NO_ISSUER_BUS_REF, f"ingested ref == {NO_ISSUER_BUS_REF!r} — got {t['tender_ref']!r}")
    _check(t["source_key"] == "dtc", f"tender.source_key == 'dtc' — got {t['source_key']!r}")
    _check(t["issuing_org_id"] is None,
           "bus row naming no body -> issuing_org_id NULL (NOT presumed DTC)")

    ev = conn.execute(
        "SELECT source_key FROM tender_events WHERE tender_id=? AND event_type='issued'", (t["id"],)
    ).fetchall()
    _check(len(ev) == 1 and ev[0]["source_key"] == "dtc",
           "one 'issued' event with source_key='dtc'")

    dang = conn.execute(
        """SELECT source_record_id, reference_type, resolution_status, referenced_entity
           FROM dangling_references WHERE source_record_table='tenders'"""
    ).fetchall()
    _check(len(dang) == 1, f"exactly 1 tenders-issuer dangling row — got {len(dang)}")
    d = dang[0]
    _check(d["source_record_id"] == t["id"] and d["reference_type"] == "org_name"
           and d["resolution_status"] == "unresolved" and d["referenced_entity"] == NO_ISSUER_BUS_REF,
           "unresolved issuer logged: ('tenders', <tender id>, 'org_name', 'unresolved')")

    # Idempotency: a second identical run writes nothing new.
    before = (conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0],
              conn.execute("SELECT COUNT(*) FROM tender_events").fetchone()[0],
              conn.execute("SELECT COUNT(*) FROM dangling_references").fetchone()[0])
    dtc.process(conn, dry_run=False, tenders_html=html)
    after = (conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM tender_events").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM dangling_references").fetchone()[0])
    _check(before == after, f"re-run is idempotent — counts unchanged {before} == {after}")


def main() -> int:
    print("=== DTC scraper offline test (REAL feed HTML, in-memory DB, no network, no data/evbus.db) ===")
    test_parser_and_dedupe_on_real_data()
    test_bus_row_ingest_unresolved_issuer()
    print("=== ALL DTC FIXTURE ASSERTIONS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
