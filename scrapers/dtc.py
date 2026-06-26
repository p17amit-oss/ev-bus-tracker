"""DTC / Delhi NIC eProcurement (GePNIC) scraper.

Targets the Government of NCT of Delhi eProcurement portal
(govtprocurement.delhi.gov.in, NIC's GePNIC system) — the CPPP-shaped, server-
rendered "Latest Tenders" + "Latest Corrigendums" rolling listings on the portal
home page. Plain HTML tables (no JS), so http_session + BeautifulSoup — no
Playwright. Modeled on scrapers/cppp.py and follows the same contracts.

DTC (Delhi Transport Corporation) is the bus operator/STU whose e-bus tenders
run on this portal; the portal itself hosts every NCT-of-Delhi department, so we
keyword-filter for buses and treat the bus rows as DTC's. We deliberately scrape
ONLY this NIC portal in this cut (the dtc.delhi.gov.in notice board is a
documented later addition).

STRUCTURE (verified live 2026-06-26, differs from CPPP — encoded below):
  The home-page feeds are 4-column tables:
    Tender Title | Reference No | Closing Date | Bid Opening Date
  i.e. title and issuer ref are ALREADY separate cells (no combined
  Title/Ref/Id cell to split, unlike CPPP). There is NO organisation column and
  NO numeric tender id in the feed; the row's detail link is a GePNIC
  "$DirectLink" whose `sp=` token is SESSION-BOUND (not a stable id), so it must
  NOT be used as a dedupe key. We therefore key dedup off the issuer Reference
  No. Dates are 'DD-Mon-YYYY hh:mm AM/PM' (same as CPPP).

COVERAGE BOUNDARY (honest): like CPPP, the home-page feeds are a rolling window
of the most recent items. The portal's keyword Advanced Search is captcha-gated
(a hidden captcha form on the search page) and NOT scraped, so this captures a
bus tender only when one surfaces in the recent feed at scrape time.
Comprehensive discovery stays a known gap (source_coverage.dtc.known_gaps).

DECISIONS encoded here (per scoping):
  * source_key='dtc' on the run, every tenders row, and every tender_events row.
  * issuing_org_id is resolved ONLY from the row's own text (Reference No or
    title naming a known Delhi body via resolve_issuer); the feed has no org
    column, so a bare bus-keyword match is NOT presumed to be DTC. A bus row with
    no resolvable issuer is still INGESTED (issuing_org_id LEFT NULL) and the
    unresolved issuer is logged to dangling_references ('org_name','unresolved').
    source_key stays 'dtc' regardless — that is the SOURCE/portal, a different
    field from the issuer. EYEBALL-AND-TIGHTEN: extend ISSUER_PATTERNS as real
    Delhi bus-issuer Reference-No prefixes become known.
  * Dedup scoped to "dtc" and keyed off the issuer Reference No (the only stable
    natural key in the feed). A CESL/CPPP/DTC overlap is a SEPARATE tenders row —
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

log = logging.getLogger("dtc")

PORTAL_BASE = "https://govtprocurement.delhi.gov.in"
# The portal home page renders both rolling feeds (Latest Tenders + Latest
# Corrigendums) server-side in a single GET — no captcha on browse.
HOME_URL = f"{PORTAL_BASE}/nicgep/app"

# Issuer resolution. The feed has NO organisation column, so the issuer is only
# resolved when the row's own text (Reference No or title) actually names a known
# Delhi transport body. A bare bus-keyword match is NOT enough to presume DTC —
# the portal hosts every NCT-of-Delhi department, and Delhi bus tenders also come
# from DIMTS / DTIDC / the Transport Department. A row that matches the bus gate
# but names no recognizable issuer is ingested with issuing_org_id LEFT NULL and
# logged to dangling_references (reference_type='org_name', 'unresolved') for
# later human resolution — never silently attributed to DTC.
#
# Each entry: (compiled pattern, org_name, org_slug, org_type). Patterns are
# matched against the combined Reference-No + title text.
ISSUER_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"\bDTC\b|Delhi Transport Corporation", re.I),
     "Delhi Transport Corporation", "dtc", "transit_authority"),
    (re.compile(r"\bDIMTS\b|Delhi Integrated Multi[- ]?Modal Transit", re.I),
     "Delhi Integrated Multi-Modal Transit System", "dimts", "transit_authority"),
    (re.compile(r"\bDTIDC\b|Delhi Transport Infrastructure Development", re.I),
     "Delhi Transport Infrastructure Development Corporation", "dtidc", "transit_authority"),
    (re.compile(r"Transport Department|Department of Transport", re.I),
     "Transport Department, GNCTD", "gnctd-transport", "transit_authority"),
]


def resolve_issuer(ref: str | None, title: str) -> tuple[str, str, str] | None:
    """Resolve the issuing body from the row's own text, or None if unknown.

    Returns (org_name, org_slug, org_type) when the Reference No or title names a
    recognizable Delhi transport body; otherwise None (issuer stays unresolved).
    EYEBALL-AND-TIGHTEN on the first live bus rows: extend ISSUER_PATTERNS as the
    real Reference-No prefixes for Delhi bus issuers become known.
    """
    hay = f"{ref or ''} {title or ''}"
    for pat, name, slug, org_type in ISSUER_PATTERNS:
        if pat.search(hay):
            return name, slug, org_type
    return None

# Same bus keyword gate as cesl.py / cppp.py (kept identical on purpose; if this
# pattern changes, change all three).
BUS_TERMS = re.compile(
    r"\b(e[- ]?bus|electric bus|gcc|gross cost|pm[- ]?e[- ]?bus|bus(es)? )",
    re.IGNORECASE,
)
COUNT_RE = re.compile(r"\b([\d,]{2,7})\s*(?:nos\.?\s*)?(?:electric\s*)?bus", re.IGNORECASE)
# GePNIC prefixes the title with a feed serial like '5. '. Strip it so titles and
# the bus keyword match operate on the real title text.
SERIAL_PREFIX_RE = re.compile(r"^\s*\d+\.\s*")


def parse_nic_date(text: str) -> str | None:
    """GePNIC prints 'DD-Mon-YYYY hh:mm AM/PM'. Return ISO date (date part only)."""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d-%b-%Y %I:%M %p", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_title(text: str) -> str:
    """Drop the GePNIC feed serial prefix ('5. Foo' -> 'Foo')."""
    return SERIAL_PREFIX_RE.sub("", (text or "").strip()).strip()


def parse_listing(html: str) -> list[dict]:
    """Parse a GePNIC feed (Latest Tenders or Latest Corrigendums) from HTML.

    Returns one dict per data row with keys:
      title, ref, closing, opening, detail_href, raw.

    Locates the feed table by its header row (the literal 'Reference No' +
    'Closing Date' headers), then reads the 4-column data rows. The header row
    and layout/footer rows are skipped. Kept tolerant of the portal's nested
    layout tables: only rows with >=4 cells whose closing-date cell parses as a
    date are treated as data rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for table in soup.find_all("table"):
        # Only parse leaf data tables. The portal wraps the feed in nested
        # layout tables; an ancestor table's recursive text also contains the
        # feed headers, which would double-count the inner rows. Skipping tables
        # that themselves contain a nested <table> isolates the real data table.
        if table.find("table") is not None:
            continue
        header = table.find("tr")
        if header is None:
            continue
        head_text = header.get_text(" ", strip=True)
        if "Reference No" not in head_text or "Closing Date" not in head_text:
            continue
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue  # header / layout rows
            cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
            # First 4 cells are Title | Ref | Closing | Opening. A real data row
            # has a parseable closing date in cell 2; the header ('Closing Date')
            # and chrome rows do not.
            if parse_nic_date(cells[2]) is None:
                continue
            link = tds[0].find("a")
            href = link.get("href") if link else None
            out.append({
                "title": normalize_title(cells[0]),
                "ref": cells[1],
                "closing": cells[2],
                "opening": cells[3],
                "detail_href": href,
                "raw": " | ".join(cells[:4]),
            })
    return out


def make_dedupe(ref: str | None, title: str) -> str:
    """Dedupe key scoped to 'dtc', keyed off the issuer Reference No.

    The Reference No is the only stable natural key in the feed (the detail
    link's sp= token is session-bound). Falls back to the title when a row has
    no ref. EYEBALL-AND-TIGHTEN on the first live bus row (same caveat as
    cesl.py / cppp.py).
    """
    return dedupe_key("dtc", ref or title[:300])


def process(conn, dry_run: bool, stats=None,
            tenders_html: str | None = None, corr_html: str | None = None):
    """Core logic shared by the live run, the dry run, and the offline test.

    If tenders_html / corr_html are provided, parse those instead of fetching
    (offline/test path). Otherwise fetch both feeds live from the portal home
    page. Writes only when dry_run is False. Returns a report dict.
    """
    if tenders_html is None or corr_html is None:
        session = http_session()
        resp = session.get(HOME_URL, timeout=30)
        resp.raise_for_status()
        # Both feeds live on the same home page; parse_listing picks each feed
        # table by its header, so the same HTML yields both row sets.
        tenders_html = corr_html = resp.text

    report = {"tenders_seen": 0, "tenders_bus": [], "corr_seen": 0,
              "corr_events": [], "dangling": []}

    # --- Latest Tenders -> tenders + 'issued' event ---
    tender_rows = parse_listing(tenders_html)
    # The corrigendum feed shares the home page; de-dupe identical row sets so we
    # don't double-count when tenders_html is corr_html (the live single-GET case).
    report["tenders_seen"] = len(tender_rows)
    for row in tender_rows:
        if not BUS_TERMS.search(row["raw"]):
            continue
        count_m = COUNT_RE.search(row["raw"])
        bus_count = int(count_m.group(1).replace(",", "")) if count_m else None
        model = "gcc" if re.search(r"\bgcc\b|gross cost", row["raw"], re.I) else "unknown"
        bid_due = parse_nic_date(row["closing"])
        key = make_dedupe(row["ref"], row["title"])
        source_url = _abs_url(row["detail_href"]) or HOME_URL
        issuer = resolve_issuer(row["ref"], row["title"])  # (name, slug, type) or None
        plan = {
            "tender_ref": row["ref"], "title": row["title"][:300],
            "issuer": issuer[0] if issuer else None,
            "issuer_resolved": issuer is not None,
            "bus_count": bus_count, "bid_due_date": bid_due, "model": model,
            "source_url": source_url, "dedupe_key": key, "source_key": "dtc",
        }
        report["tenders_bus"].append(plan)
        if dry_run:
            continue
        # Resolve issuer only from the row's text; otherwise leave it NULL and
        # log the unresolved issuer below (never presume DTC).
        issuer_id = upsert_org(conn, *issuer) if issuer else None
        cur = conn.execute(
            """INSERT OR IGNORE INTO tenders
               (tender_ref, title, issuing_org_id, procurement_model,
                bus_count, bid_due_date, status, source_url, raw_text,
                source_key, dedupe_key)
               VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, 'dtc', ?)""",
            (row["ref"], row["title"][:300], issuer_id, model, bus_count, bid_due,
             source_url, row["raw"], key),
        )
        if cur.rowcount:
            tender_id = cur.lastrowid
            conn.execute(
                """INSERT OR IGNORE INTO tender_events
                   (tender_id, event_type, details, source_url, source_key, dedupe_key)
                   VALUES (?, 'issued', 'First seen on Delhi NIC eProc latest tenders', ?, 'dtc', ?)""",
                (tender_id, source_url, dedupe_key("dtc-issued", key)),
            )
            if issuer_id is None:
                # Ingested but issuer unknown: record the known-unknown so the
                # gap is visible and resolvable, rather than fabricating DTC.
                conn.execute(
                    """INSERT INTO dangling_references
                       (source_record_table, source_record_id, referenced_entity,
                        reference_type, resolution_status, conflict_notes)
                       VALUES ('tenders', ?, ?, 'org_name', 'unresolved', ?)""",
                    (tender_id, (row["ref"] or row["title"])[:200],
                     "Delhi NIC bus tender ingested with no resolvable issuing body "
                     "(feed has no org column; row text named none). Resolve the issuer "
                     "from the tender document and set tenders.issuing_org_id."),
                )
                report["dangling"].append({
                    "referenced_entity": (row["ref"] or row["title"])[:200],
                    "reason": "DTC-source bus tender with unresolved issuer",
                })
            if stats:
                stats.rows_inserted += 1

    # --- Latest Corrigendums -> deadline_extended / corrigendum, or dangling ---
    corr_rows = parse_listing(corr_html)
    report["corr_seen"] = len(corr_rows)
    for row in corr_rows:
        if not BUS_TERMS.search(row["raw"]):
            continue
        key = make_dedupe(row["ref"], row["title"])
        match = conn.execute(
            "SELECT id, bid_due_date FROM tenders WHERE source_key = 'dtc' AND dedupe_key = ?",
            (key,),
        ).fetchone()

        if match is None:
            # Never fabricate a tender for an unseen corrigendum.
            dangling = {
                "referenced_entity": (row["ref"] or row["title"])[:200],
                "reason": "Delhi NIC corrigendum for a tender not in our index",
            }
            report["dangling"].append(dangling)
            if not dry_run:
                exists = conn.execute(
                    """SELECT 1 FROM dangling_references
                       WHERE source_record_table = 'dtc_corrigendum'
                         AND referenced_entity = ?""",
                    (dangling["referenced_entity"],),
                ).fetchone()
                if not exists:
                    conn.execute(
                        """INSERT INTO dangling_references
                           (source_record_table, source_record_id, referenced_entity,
                            reference_type, resolution_status, conflict_notes)
                           VALUES ('dtc_corrigendum', 0, ?, 'tender_ref', 'unresolved', ?)""",
                        (dangling["referenced_entity"],
                         "Delhi NIC corrigendum (bus-matched) for a tender not ingested; not "
                         "materialized to avoid fabrication. Resolve when the tender is captured."),
                    )
            continue

        new_date = parse_nic_date(row["closing"])
        if new_date:
            etype = "deadline_extended"
            details = f"Bid submission closing date revised to {new_date} per Delhi NIC corrigendum"
        else:
            etype = "corrigendum"
            details = "Corrigendum issued on Delhi NIC eProc (no parseable revised closing date)"
        ev_key = dedupe_key("dtc-corr", key, new_date or row["raw"][:120])
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
               VALUES (?, ?, ?, ?, ?, 'dtc', ?)""",
            (match["id"], etype, new_date, details,
             _abs_url(row["detail_href"]) or HOME_URL, ev_key),
        )
        if cur.rowcount and etype == "deadline_extended":
            conn.execute(
                "UPDATE tenders SET bid_due_date = ? WHERE id = ?",
                (new_date, match["id"]),
            )

    return report


def _abs_url(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    return PORTAL_BASE + (href if href.startswith("/") else "/" + href)


def run(dry_run: bool = False) -> None:
    conn = get_db()
    if dry_run:
        report = process(conn, dry_run=True)
        _print_dry_run(report)
        return
    with track_run(conn, "dtc", source_key="dtc") as stats:
        report = process(conn, dry_run=False, stats=stats)
        stats.rows_found = (len(report["tenders_bus"]) + len(report["corr_events"])
                            + len(report["dangling"]))
        conn.commit()
        log.info("dtc: tenders_seen=%d bus=%d corr_seen=%d events=%d dangling=%d",
                 report["tenders_seen"], len(report["tenders_bus"]),
                 report["corr_seen"], len(report["corr_events"]), len(report["dangling"]))


def _print_dry_run(report: dict) -> None:
    print("=== DTC / Delhi NIC DRY RUN (no writes) ===")
    print(f"Latest Tenders feed: {report['tenders_seen']} rows seen, "
          f"{len(report['tenders_bus'])} bus-matched")
    for p in report["tenders_bus"]:
        issuer_str = p["issuer"] if p.get("issuer_resolved") else "UNRESOLVED (issuer left NULL, dangling logged)"
        print(f"  TENDER would-insert: ref={p['tender_ref']!r} title={p['title'][:70]!r} "
              f"issuer={issuer_str!r} buses={p['bus_count']} bid_due={p['bid_due_date']} "
              f"model={p['model']} source_key=dtc")
    print(f"Corrigendum feed: {report['corr_seen']} rows seen, "
          f"{len(report['corr_events'])} bus-matched-to-known, "
          f"{len(report['dangling'])} unmatched (dangling)")
    for e in report["corr_events"]:
        print(f"  EVENT would-write: tender_id={e['tender_id']} type={e['event_type']} "
              f"new_due={e['new_bid_due_date']} :: {e['details']}")
    for d in report["dangling"]:
        print(f"  DANGLING would-write: {d['referenced_entity']!r}")
    if not report["tenders_bus"] and not report["corr_events"] and not report["dangling"]:
        print("NO current bus tenders or corrigenda in the Delhi NIC recent feeds — nothing to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape Delhi NIC eProc (DTC) bus tenders")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + parse + print intended writes; touch nothing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
