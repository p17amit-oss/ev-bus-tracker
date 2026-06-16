"""CESL (Convergence Energy Services Ltd) tender page scraper.

CESL runs the big demand-aggregation e-bus tenders (Grand Challenge,
PM e-Bus Sewa lots). Their tender listing is JS-rendered, so we use
Playwright, filter listings for bus-related terms, and upsert into
`tenders` + an 'issued' row in `tender_events`.

Selector strategy: the page structure changes; we scrape generically —
every row/card under the tenders container — and keyword-filter the text,
rather than depending on exact CSS classes. First live run should be
eyeballed and selectors tightened in TENDER_ROW_SELECTORS.
"""

from __future__ import annotations

import argparse
import logging
import re

from playwright.sync_api import sync_playwright

from common import dedupe_key, get_db, track_run, upsert_org

log = logging.getLogger("cesl")

# Validated live 2026-06-11: the listing is a plain table at /tender with
# columns: S.No | Description | NIT/RFP Number | E-Tender ID | Sale Start | Sale End.
TENDER_URLS = [
    "https://www.convergence.co.in/tender",
    "https://www.convergence.co.in/public/tenders",  # fallback: old path
]

# Tried in order; first selector that yields nodes wins.
TENDER_ROW_SELECTORS = [
    "table tbody tr",
    ".tender-list .tender-item",
    ".card:has-text('Tender')",
]

BUS_TERMS = re.compile(
    r"\b(e[- ]?bus|electric bus|gcc|gross cost|pm[- ]?e[- ]?bus|bus(es)? )",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})")
COUNT_RE = re.compile(r"\b([\d,]{2,7})\s*(?:nos\.?\s*)?(?:electric\s*)?bus", re.IGNORECASE)
# Refs end in a slash-delimited digit run (e.g. CESL/06/.../262704003); the
# trailing /\d+ anchor stops the match before e-tender IDs and dd-mm-yyyy
# dates. Would over-match if CESL ever prints dates with slashes in this cell.
REF_RE = re.compile(r"\b(CESL/[\w.\- /]{4,80}/\d+)\b")


def last_date(text: str) -> str | None:
    """Last dd-mm-yyyy style date in a row = tender sale end date, in ISO."""
    matches = DATE_RE.findall(text)
    if not matches:
        return None
    d, mth, y = re.split(r"[-/.]", matches[-1])
    if len(y) == 2:
        y = "20" + y
    try:
        return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"
    except ValueError:
        return None


def extract_rows(page) -> list[dict]:
    """Pull text + first link from whatever listing structure is present."""
    for selector in TENDER_ROW_SELECTORS:
        nodes = page.locator(selector)
        if nodes.count() > 0:
            rows = []
            for i in range(nodes.count()):
                node = nodes.nth(i)
                text = " ".join(node.inner_text().split())
                href = None
                links = node.locator("a")
                if links.count() > 0:
                    href = links.first.get_attribute("href")
                if text:
                    rows.append({"text": text, "href": href})
            log.info("selector %r matched %d rows", selector, len(rows))
            return rows
    return []


def run(headless: bool = True) -> None:
    conn = get_db()
    cesl_id = upsert_org(conn, "Convergence Energy Services Ltd", "cesl", "agency",
                         website="https://www.convergence.co.in")

    with track_run(conn, "cesl", source_key="cesl") as stats, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()
        rows: list[dict] = []
        last_error = None
        for url in TENDER_URLS:
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
                rows = extract_rows(page)
                if rows:
                    base_url = url
                    break
            except Exception as exc:
                last_error = exc
                log.warning("URL %s failed: %s", url, exc)
        browser.close()

        if not rows and last_error:
            raise RuntimeError(f"all CESL tender URLs failed: {last_error}")

        stats.rows_found = len(rows)
        for row in rows:
            text = row["text"]
            if not BUS_TERMS.search(text):
                continue
            href = row["href"] or ""
            if href and not href.startswith("http"):
                href = "https://www.convergence.co.in" + (
                    href if href.startswith("/") else "/" + href
                )
            count_match = COUNT_RE.search(text)
            bus_count = (
                int(count_match.group(1).replace(",", "")) if count_match else None
            )
            ref_match = REF_RE.search(text)
            tender_ref = ref_match.group(1).strip() if ref_match else None
            model = "gcc" if re.search(r"\bgcc\b|gross cost", text, re.I) else "unknown"
            # Dedupe on the ref number when present so description edits or
            # corrigenda don't create duplicate tenders.
            key = dedupe_key("cesl", tender_ref or text[:300])
            cur = conn.execute(
                """INSERT OR IGNORE INTO tenders
                   (tender_ref, title, issuing_org_id, procurement_model,
                    bus_count, bid_due_date, status, source_url, raw_text,
                    dedupe_key)
                   VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (tender_ref, text[:300], cesl_id, model, bus_count,
                 last_date(text), href or base_url, text, key),
            )
            if cur.rowcount:
                tender_id = cur.lastrowid
                conn.execute(
                    """INSERT OR IGNORE INTO tender_events
                       (tender_id, event_type, details, source_url, dedupe_key)
                       VALUES (?, 'issued', 'First seen on CESL tender page', ?, ?)""",
                    (tender_id, href or base_url, dedupe_key("cesl-issued", key)),
                )
                stats.rows_inserted += 1
        conn.commit()
        log.info("cesl: found=%d inserted=%d", stats.rows_found, stats.rows_inserted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape CESL tenders")
    parser.add_argument("--headed", action="store_true", help="run with visible browser")
    args = parser.parse_args()
    run(headless=not args.headed)
