"""Offline fixture test for scrapers/dtc.py.

Runs entirely against saved NIC HTML fixtures and an IN-MEMORY SQLite DB — it
never touches data/evbus.db and never hits the network. Mirrors how the DTC
scraper would run live, but with the two home-page feeds supplied as HTML.

Run directly:  python3 tests/test_dtc.py
Or via pytest:  pytest tests/test_dtc.py

Assertions:
  1. parse_listing finds all data rows and skips header/chrome rows.
  2. The bus keyword gate keeps bus rows and drops non-bus rows.
  3. tender_ref is taken from the Reference No cell; the serial-prefixed title
     is normalized.
  4. dedupe_key is stable across two parses of the same row.
  5. A full process() run writes tenders + 'issued' events with source_key='dtc'
     to an in-memory DB, is idempotent on re-run, records a matched bus
     corrigendum as deadline_extended (updating bid_due_date), and files an
     unmatched bus corrigendum as a dangling_reference (never a fabricated tender).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

import dtc  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA = (REPO_ROOT / "db" / "schema.sql").read_text()

TENDERS_HTML = (FIXTURES / "dtc_latest_tenders.html").read_text()
CORR_HTML = (FIXTURES / "dtc_latest_corrigendums.html").read_text()

BUS_REF = "DTC/EV/2026-27/05"
UNMATCHED_BUS_REF = "DTC/EV/2025-26/99"


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


def test_parse_and_filter():
    print("1-4. parse / filter / ref / dedupe stability")
    rows = dtc.parse_listing(TENDERS_HTML)
    _check(len(rows) == 3, f"parse_listing found 3 data rows (header+chrome skipped) — got {len(rows)}")

    bus = [r for r in rows if dtc.BUS_TERMS.search(r["raw"])]
    nonbus = [r for r in rows if not dtc.BUS_TERMS.search(r["raw"])]
    _check(len(bus) == 1, f"exactly 1 bus row passes the keyword gate — got {len(bus)}")
    _check(len(nonbus) == 2, f"2 non-bus rows filtered out — got {len(nonbus)}")

    b = bus[0]
    _check(b["ref"] == BUS_REF, f"tender_ref from Reference No cell == {BUS_REF!r} — got {b['ref']!r}")
    _check(b["title"].startswith("Procurement and Operation of 100 Electric Buses"),
           f"serial prefix stripped from title — got {b['title'][:40]!r}")
    _check(dtc.parse_nic_date(b["closing"]) == "2026-08-10",
           f"closing date parsed to ISO 2026-08-10 — got {dtc.parse_nic_date(b['closing'])!r}")

    # dedupe stability: same row parsed twice -> identical key
    rows2 = dtc.parse_listing(TENDERS_HTML)
    b2 = [r for r in rows2 if dtc.BUS_TERMS.search(r["raw"])][0]
    k1 = dtc.make_dedupe(b["ref"], b["title"])
    k2 = dtc.make_dedupe(b2["ref"], b2["title"])
    _check(k1 == k2, f"dedupe_key stable across two parses — {k1} == {k2}")


def test_process_writes_and_idempotency():
    print("5. process() against in-memory DB (writes, idempotency, corrigendum, dangling)")
    conn = _mem_db()

    report = dtc.process(conn, dry_run=False, tenders_html=TENDERS_HTML, corr_html=CORR_HTML)

    # One bus tender written with source_key='dtc'
    trows = conn.execute("SELECT id, tender_ref, bus_count, bid_due_date, source_key FROM tenders").fetchall()
    _check(len(trows) == 1, f"exactly 1 tender written — got {len(trows)}")
    t = trows[0]
    _check(t["source_key"] == "dtc", f"tender.source_key == 'dtc' — got {t['source_key']!r}")
    _check(t["tender_ref"] == BUS_REF, f"tender.tender_ref == {BUS_REF!r} — got {t['tender_ref']!r}")
    _check(t["bus_count"] == 100, f"bus_count parsed == 100 — got {t['bus_count']}")

    # 'issued' event with source_key='dtc'
    issued = conn.execute(
        "SELECT source_key FROM tender_events WHERE tender_id=? AND event_type='issued'", (t["id"],)
    ).fetchall()
    _check(len(issued) == 1 and issued[0]["source_key"] == "dtc",
           "one 'issued' event written with source_key='dtc'")

    # Matched bus corrigendum -> deadline_extended + bid_due_date updated to 2026-08-20
    ext = conn.execute(
        "SELECT event_date FROM tender_events WHERE tender_id=? AND event_type='deadline_extended'", (t["id"],)
    ).fetchall()
    _check(len(ext) == 1 and ext[0]["event_date"] == "2026-08-20",
           "matched bus corrigendum -> deadline_extended with revised date 2026-08-20")
    t_after = conn.execute("SELECT bid_due_date FROM tenders WHERE id=?", (t["id"],)).fetchone()
    _check(t_after["bid_due_date"] == "2026-08-20",
           f"tender.bid_due_date updated to 2026-08-20 — got {t_after['bid_due_date']!r}")

    # Unmatched bus corrigendum -> dangling_reference, NOT a fabricated tender
    dang = conn.execute(
        "SELECT referenced_entity FROM dangling_references WHERE source_record_table='dtc_corrigendum'"
    ).fetchall()
    _check(len(dang) == 1 and dang[0]["referenced_entity"] == UNMATCHED_BUS_REF,
           f"unmatched bus corrigendum filed as dangling ({UNMATCHED_BUS_REF}), not fabricated")
    _check(conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0] == 1,
           "still exactly 1 tender — unmatched corrigendum did NOT create a tender")

    # report shape
    _check(report["dangling"] and report["corr_events"],
           "report records both a corrigendum event and a dangling reference")

    # Idempotency: a second identical run writes nothing new
    before = (conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0],
              conn.execute("SELECT COUNT(*) FROM tender_events").fetchone()[0],
              conn.execute("SELECT COUNT(*) FROM dangling_references").fetchone()[0])
    dtc.process(conn, dry_run=False, tenders_html=TENDERS_HTML, corr_html=CORR_HTML)
    after = (conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM tender_events").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM dangling_references").fetchone()[0])
    _check(before == after, f"re-run is idempotent — counts unchanged {before} == {after}")


def main() -> int:
    print("=== DTC scraper offline fixture test (in-memory DB, no network, no data/evbus.db) ===")
    test_parse_and_filter()
    test_process_writes_and_idempotency()
    print("=== ALL DTC FIXTURE ASSERTIONS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
