"""DTC / Delhi NIC eProcurement (GePNIC) scraper — Latest-Tenders feed only.

Targets the Government of NCT of Delhi eProcurement portal
(govtprocurement.delhi.gov.in, NIC's GePNIC system) — the server-rendered
"Latest Tenders" rolling listing on the portal home page. Plain HTML (no JS), so
http_session + BeautifulSoup — no Playwright. Modeled on scrapers/cppp.py.

DTC (Delhi Transport Corporation) is the bus operator/STU whose e-bus tenders
run on this portal; the portal hosts every NCT-of-Delhi department, so we
keyword-filter for buses. We deliberately scrape ONLY this NIC portal's
Latest-Tenders feed in this cut (the dtc.delhi.gov.in notice board is a
documented later addition).

STRUCTURE (verified live 2026-06-26): the home page renders the feed as a
4-column listing — Tender Title | Reference No | Closing Date | Bid Opening Date
— but the data rows live in a NESTED <table id="activeTenders"> inside marquee
<div>s, under a header row whose own cells are the column labels. The feed
heading ("Latest Tenders updates every 15 mins.") sits in the SAME enclosing
table. So we anchor on that heading, take its enclosing data-bearing table, and
read every data row once (a homepage-wide row scan would mix feeds and
double-count across the nested layout). Dates are 'DD-Mon-YYYY hh:mm AM/PM'.

CORRIGENDUM FEED DROPPED (documented, not a silent drop): the portal's Latest-
Corrigendums feed identifies tenders by the issuer NIT ref (e.g.
'NIT No.04/2026-27/CD-11(IA)'), a DIFFERENT namespace from the Latest-Tenders
feed's GePNIC system codes (e.g. 'T25R220484'). The two do not bridge, so a
corrigendum cannot be linked to its parent tender by ref. Corrigendum capture is
deferred until a real linkage mechanism exists (e.g. PDF-level parent-id
extraction). Recorded in source_coverage.dtc.known_gaps.

COVERAGE BOUNDARY (honest): the feed is a rolling window of the most recent
items. The portal's keyword Advanced Search is captcha-gated and NOT scraped, so
this captures a bus tender only when one surfaces in the recent feed at scrape
time. Comprehensive discovery stays a known gap (source_coverage.dtc.known_gaps).

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
  * Dedup scoped to 'dtc' keyed off normalized Reference No + title (NOT ref
    alone): live data showed three distinct tenders sharing one NIT ref, the
    distinguishing item suffix living only in the title. See make_dedupe.
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
# The portal home page renders the Latest-Tenders feed server-side in one GET —
# no captcha on browse.
HOME_URL = f"{PORTAL_BASE}/nicgep/app"

# The feed is introduced by this heading; its enclosing data-bearing table scopes
# exactly the Latest-Tenders feed.
TENDERS_HEADING = re.compile(r"Latest Tenders updates", re.I)

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
    """Drop the GePNIC feed serial prefix ('5. Foo') and collapse whitespace."""
    s = SERIAL_PREFIX_RE.sub("", (text or "").strip())
    return re.sub(r"\s+", " ", s).strip()


def make_dedupe(ref: str | None, title: str) -> str:
    """Dedupe key scoped to 'dtc', keyed off normalized Reference No + title.

    ref+title (not ref alone) because the Reference No is NOT unique within the
    feed — live data showed three distinct tenders sharing one NIT ref
    ('NIT No 7 (2026-27) EE (C)-37'), the distinguishing item suffix living only
    in the title. Title normalization (serial-prefix strip + whitespace collapse
    + case-fold via dedupe_key) guards against trivial re-list churn
    (whitespace/case/serial-prefix changes) producing false NEW rows.

    CAVEAT: still untightened against real BUS rows — the live window had zero —
    so revisit on the first live bus capture.
    """
    norm_ref = re.sub(r"\s+", " ", (ref or "").strip())
    return dedupe_key("dtc", f"{norm_ref}|{normalize_title(title)}")


def _row_cells(tr) -> list[str] | None:
    """A feed DATA row has >=4 direct cells whose closing-date cell parses.

    Returns the cell texts when tr is a data row, else None. Using direct cells
    (recursive=False) rejects the header row ('Closing Date' literal), chrome
    rows, and nested-layout wrapper rows (one big cell spanning the feed).
    """
    tds = tr.find_all("td", recursive=False)
    if len(tds) < 4:
        return None
    cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
    if parse_nic_date(cells[2]) is None:
        return None
    return cells


def _feed_container(soup):
    """Return the nearest ancestor table that holds the Latest-Tenders rows.

    GePNIC renders the feed heading and its (nested) data list inside the same
    enclosing table; that table scopes exactly this feed.
    """
    node = soup.find(string=TENDERS_HEADING)
    if node is None:
        return None
    anc = node
    for _ in range(6):
        anc = anc.find_parent(["td", "table"])
        if anc is None:
            return None
        if any(_row_cells(tr) for tr in anc.find_all("tr")):
            return anc
    return None


def parse_listing(html: str) -> list[dict]:
    """Parse the GePNIC home-page Latest-Tenders feed from HTML.

    Returns one dict per data row with keys: title, ref, closing, opening,
    detail_href, raw. Scopes to the feed's enclosing table (see _feed_container)
    and reads each physical data row once — no nested-table double-counting. The
    header and chrome/footer rows are skipped by _row_cells.
    """
    soup = BeautifulSoup(html, "html.parser")
    container = _feed_container(soup)
    if container is None:
        return []
    out: list[dict] = []
    for tr in container.find_all("tr"):
        cells = _row_cells(tr)
        if cells is None:
            continue
        link = tr.find_all("td", recursive=False)[0].find("a")
        href = link.get("href") if link else None
        out.append({
            "title": normalize_title(cells[0]),
            "ref": cells[1].strip(),
            "closing": cells[2],
            "opening": cells[3],
            "detail_href": href,
            "raw": " | ".join(cells[:4]),
        })
    return out


def _abs_url(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    return PORTAL_BASE + (href if href.startswith("/") else "/" + href)


def process(conn, dry_run: bool, stats=None, tenders_html: str | None = None):
    """Core logic shared by the live run, the dry run, and the offline test.

    If tenders_html is provided, parse it instead of fetching (offline/test
    path). Otherwise fetch the Latest-Tenders feed live from the home page.
    Writes only when dry_run is False. Returns a report dict.

    Tenders-feed ONLY: the corrigendum feed is intentionally not fetched or
    processed (non-bridging ref namespace — see module docstring).
    """
    if tenders_html is None:
        session = http_session()
        resp = session.get(HOME_URL, timeout=30)
        resp.raise_for_status()
        tenders_html = resp.text

    report = {"tenders_seen": 0, "tenders_bus": [], "dangling": []}

    tender_rows = parse_listing(tenders_html)
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

    return report


def run(dry_run: bool = False) -> None:
    conn = get_db()
    if dry_run:
        report = process(conn, dry_run=True)
        _print_dry_run(report)
        return
    with track_run(conn, "dtc", source_key="dtc") as stats:
        report = process(conn, dry_run=False, stats=stats)
        stats.rows_found = len(report["tenders_bus"]) + len(report["dangling"])
        conn.commit()
        log.info("dtc: tenders_seen=%d bus=%d dangling=%d",
                 report["tenders_seen"], len(report["tenders_bus"]),
                 len(report["dangling"]))


def _print_dry_run(report: dict) -> None:
    print("=== DTC / Delhi NIC DRY RUN (Latest-Tenders feed only, no writes) ===")
    print(f"Latest Tenders feed: {report['tenders_seen']} rows seen, "
          f"{len(report['tenders_bus'])} bus-matched")
    for p in report["tenders_bus"]:
        issuer_str = p["issuer"] if p.get("issuer_resolved") else "UNRESOLVED (issuer NULL, dangling logged)"
        print(f"  TENDER would-insert: ref={p['tender_ref']!r} title={p['title'][:70]!r} "
              f"issuer={issuer_str!r} buses={p['bus_count']} bid_due={p['bid_due_date']} "
              f"model={p['model']} source_key=dtc")
    if not report["tenders_bus"]:
        print("NO current bus tenders in the Delhi NIC Latest-Tenders window — nothing to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape Delhi NIC eProc (DTC) bus tenders — tenders feed only")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + parse + print intended writes; touch nothing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
