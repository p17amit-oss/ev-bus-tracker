"""CPPP (Central Public Procurement Portal, eprocure.gov.in) scraper.

CPPP is a broad cross-government tender aggregator. We poll its two public,
server-rendered feeds and keyword-filter for buses:

  * Active Tenders  -> https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata
  * Corrigendum     -> https://eprocure.gov.in/cppp/latestactivecorrigendumsnew

Both are plain HTML tables (no JS), so we use http_session + BeautifulSoup —
no Playwright needed. Columns on both feeds:
  Sl.No | e-Published Date | Bid Submission Closing Date | Tender Opening Date
        | Title/Ref.No./Tender Id | Organisation Name | Corrigendum

COVERAGE BOUNDARY (honest): these feeds are a rolling window of the most
recent ~10 items. CPPP's keyword search (the only way to target "bus"
specifically) is CAPTCHA-gated and NOT scraped — so this captures a bus tender
only when one surfaces in the recent feed at scrape time. Comprehensive CPPP
bus discovery stays a known gap (source_coverage.cppp.known_gaps).

DECISIONS encoded here (per scoping):
  * source_key='cppp' on the run, every tenders row, and every tender_events row.
  * issuing_org_id = the named issuing authority (upsert_org), NOT CPPP — CPPP
    is the *source*, the listed org is the *issuer*.
  * Dedup scoped to "cppp"; a CESL/CPPP overlap is a SEPARATE tenders row —
    grouping is a later pipeline (clustering, not merge).
  * Corrigendum -> 'deadline_extended' (and update bid_due_date) ONLY when a new
    closing date is explicitly parseable; otherwise 'corrigendum' (date
    untouched); ambiguous -> 'corrigendum'.
  * A corrigendum referencing a tender we have not ingested -> dangling_references
    row, never a fabricated tender.
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup

from common import dedupe_key, get_db, http_session, track_run, upsert_org

log = logging.getLogger("cppp")

ACTIVE_URL = "https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata"
CORRIGENDUM_URL = "https://eprocure.gov.in/cppp/latestactivecorrigendumsnew"

# Same bus keyword gate as cesl.py (kept identical on purpose; if this pattern
# changes, change both).
BUS_TERMS = re.compile(
    r"\b(e[- ]?bus|electric bus|gcc|gross cost|pm[- ]?e[- ]?bus|bus(es)? )",
    re.IGNORECASE,
)
COUNT_RE = re.compile(r"\b([\d,]{2,7})\s*(?:nos\.?\s*)?(?:electric\s*)?bus", re.IGNORECASE)
# Trailing numeric run in the Title/Ref cell is CPPP's tender id (e.g. .../159140).
TENDER_ID_RE = re.compile(r"(\d{5,})\s*$")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "org"


def parse_cppp_date(text: str) -> str | None:
    """CPPP prints 'DD-Mon-YYYY hh:mm AM/PM'. Return ISO date (date part only)."""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d-%b-%Y %I:%M %p", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def fetch_rows(session, url: str) -> list[dict]:
    """Return one dict per data row of the CPPP feed table.

    Keys: published, closing, opening, title_cell, org, detail_href, raw.
    Header row and any short/footer rows are skipped.
    """
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[dict] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue  # header / layout rows
            cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
            title_td = tds[4]
            link = title_td.find("a")
            href = link.get("href") if link else None
            out.append({
                "published": cells[1],
                "closing": cells[2],
                "opening": cells[3],
                "title_cell": cells[4],
                "org": cells[5],
                "detail_href": href,
                "raw": " | ".join(cells),
            })
    return out


def split_title_ref(title_cell: str) -> tuple[str, str | None, str | None]:
    """Best-effort split of the 'Title /Ref.No./Tender Id' cell.

    Returns (title, tender_ref, tender_id). The cell concatenates the title,
    the issuer ref, and CPPP's numeric tender id; first live bus match should
    be eyeballed and this tightened (same caveat as cesl.py selectors).
    """
    tid_match = TENDER_ID_RE.search(title_cell)
    tender_id = tid_match.group(1) if tid_match else None
    # Title is the part before the first ' /' separator; the remainder is ref/id.
    parts = title_cell.split(" /", 1)
    title = parts[0].strip()
    tender_ref = parts[1].strip() if len(parts) > 1 else None
    return title, tender_ref, tender_id


def process(conn, dry_run: bool, stats=None):
    """Core logic, shared by the live run and the dry run.

    Returns a report dict of what was (or would be) written.
    """
    session = http_session()
    report = {"active_seen": 0, "active_bus": [], "corr_seen": 0,
              "corr_events": [], "dangling": []}

    # --- Active Tenders -> tenders + 'issued' event ---
    active = fetch_rows(session, ACTIVE_URL)
    report["active_seen"] = len(active)
    for row in active:
        if not BUS_TERMS.search(row["raw"]):
            continue
        title, tender_ref, tender_id = split_title_ref(row["title_cell"])
        count_m = COUNT_RE.search(row["raw"])
        bus_count = int(count_m.group(1).replace(",", "")) if count_m else None
        model = "gcc" if re.search(r"\bgcc\b|gross cost", row["raw"], re.I) else "unknown"
        bid_due = parse_cppp_date(row["closing"])
        key = dedupe_key("cppp", tender_id or tender_ref or row["title_cell"][:300])
        source_url = row["detail_href"] or ACTIVE_URL
        org_name = row["org"] or "Unknown issuer"
        plan = {
            "tender_ref": tender_ref, "title": title[:300], "issuer": org_name,
            "bus_count": bus_count, "bid_due_date": bid_due, "model": model,
            "source_url": source_url, "dedupe_key": key, "source_key": "cppp",
        }
        report["active_bus"].append(plan)
        if dry_run:
            continue
        issuer_id = upsert_org(conn, org_name, slugify(org_name), "other")
        cur = conn.execute(
            """INSERT OR IGNORE INTO tenders
               (tender_ref, title, issuing_org_id, procurement_model,
                bus_count, bid_due_date, status, source_url, raw_text,
                source_key, dedupe_key)
               VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, 'cppp', ?)""",
            (tender_ref, title[:300], issuer_id, model, bus_count, bid_due,
             source_url, row["raw"], key),
        )
        if cur.rowcount:
            conn.execute(
                """INSERT OR IGNORE INTO tender_events
                   (tender_id, event_type, details, source_url, source_key, dedupe_key)
                   VALUES (?, 'issued', 'First seen on CPPP active tenders', ?, 'cppp', ?)""",
                (cur.lastrowid, source_url, dedupe_key("cppp-issued", key)),
            )
            if stats:
                stats.rows_inserted += 1

    # --- Corrigendum -> deadline_extended / corrigendum, or dangling ---
    corr = fetch_rows(session, CORRIGENDUM_URL)
    report["corr_seen"] = len(corr)
    for row in corr:
        if not BUS_TERMS.search(row["raw"]):
            continue
        title, tender_ref, tender_id = split_title_ref(row["title_cell"])
        key = dedupe_key("cppp", tender_id or tender_ref or row["title_cell"][:300])
        match = conn.execute(
            "SELECT id, bid_due_date FROM tenders WHERE source_key = 'cppp' AND dedupe_key = ?",
            (key,),
        ).fetchone()

        if match is None:
            # Never fabricate a tender for an unseen corrigendum.
            dangling = {
                "referenced_entity": (tender_ref or title)[:200],
                "tender_id": tender_id, "reason": "CPPP corrigendum for a tender not in our index",
            }
            report["dangling"].append(dangling)
            if not dry_run:
                exists = conn.execute(
                    """SELECT 1 FROM dangling_references
                       WHERE source_record_table = 'cppp_corrigendum'
                         AND referenced_entity = ?""",
                    (dangling["referenced_entity"],),
                ).fetchone()
                if not exists:
                    conn.execute(
                        """INSERT INTO dangling_references
                           (source_record_table, source_record_id, referenced_entity,
                            reference_type, resolution_status, conflict_notes)
                           VALUES ('cppp_corrigendum', ?, ?, 'tender_ref', 'unresolved', ?)""",
                        (int(tender_id) if tender_id else 0, dangling["referenced_entity"],
                         "CPPP corrigendum (bus-matched) for a tender not ingested; not "
                         "materialized to avoid fabrication. Resolve when the tender is captured."),
                    )
            continue

        new_date = parse_cppp_date(row["closing"])
        if new_date:
            etype = "deadline_extended"
            details = f"Bid submission closing date revised to {new_date} per CPPP corrigendum"
        else:
            etype = "corrigendum"
            details = "Corrigendum issued on CPPP (no parseable revised closing date)"
        ev_key = dedupe_key("cppp-corr", key, new_date or row["raw"][:120])
        report["corr_events"].append({
            "tender_id": match["id"], "event_type": etype,
            "new_bid_due_date": new_date if etype == "deadline_extended" else None,
            "details": details,
        })
        if dry_run:
            continue
        cur = conn.execute(
            """INSERT OR IGNORE INTO tender_events
               (tender_id, event_type, event_date, details, source_url, source_key, dedupe_key)
               VALUES (?, ?, ?, ?, ?, 'cppp', ?)""",
            (match["id"], etype, new_date, details, row["detail_href"] or CORRIGENDUM_URL, ev_key),
        )
        if cur.rowcount and etype == "deadline_extended":
            conn.execute(
                "UPDATE tenders SET bid_due_date = ? WHERE id = ?",
                (new_date, match["id"]),
            )

    return report


def run(dry_run: bool = False) -> None:
    conn = get_db()
    if dry_run:
        report = process(conn, dry_run=True)
        _print_dry_run(report)
        return
    with track_run(conn, "cppp", source_key="cppp") as stats:
        report = process(conn, dry_run=False, stats=stats)
        stats.rows_found = len(report["active_bus"]) + len(report["corr_events"]) + len(report["dangling"])
        conn.commit()
        log.info("cppp: active_seen=%d bus=%d corr_seen=%d events=%d dangling=%d",
                 report["active_seen"], len(report["active_bus"]),
                 report["corr_seen"], len(report["corr_events"]), len(report["dangling"]))


def _print_dry_run(report: dict) -> None:
    print("=== CPPP DRY RUN (no writes) ===")
    print(f"Active Tenders feed: {report['active_seen']} rows seen, "
          f"{len(report['active_bus'])} bus-matched")
    for p in report["active_bus"]:
        print(f"  TENDER would-insert: ref={p['tender_ref']!r} title={p['title'][:70]!r} "
              f"issuer={p['issuer']!r} buses={p['bus_count']} bid_due={p['bid_due_date']} "
              f"model={p['model']} source_key=cppp")
    print(f"Corrigendum feed: {report['corr_seen']} rows seen, "
          f"{len(report['corr_events'])} bus-matched-to-known, "
          f"{len(report['dangling'])} unmatched (dangling)")
    for e in report["corr_events"]:
        print(f"  EVENT would-write: tender_id={e['tender_id']} type={e['event_type']} "
              f"new_due={e['new_bid_due_date']} :: {e['details']}")
    for d in report["dangling"]:
        print(f"  DANGLING would-write: {d['referenced_entity']!r} (tender_id={d['tender_id']})")
    if not report["active_bus"] and not report["corr_events"] and not report["dangling"]:
        print("NO current bus tenders or corrigenda in the CPPP recent feeds — nothing to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape CPPP (eprocure.gov.in) bus tenders")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + parse + print intended writes; touch nothing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
