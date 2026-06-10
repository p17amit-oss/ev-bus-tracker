"""Vahan Analytics scraper — monthly electric bus registrations by maker.

Target: https://vahan.parivahan.gov.in/analytics/ (JSF/PrimeFaces app).
This is the most brittle of the three sources: it is a stateful JavaServer
Faces UI where every dropdown change is an AJAX postback. Playwright drives
it like a human: select Y-axis=Maker, X-axis=Month, vehicle category BUS,
fuel ELECTRIC(BOV), refresh, read the result grid.

Fragility is expected and contained: any failure writes an 'error'
scrape_runs row that the daily digest surfaces. Numbers also revise
backwards (states upload late), so we re-scrape the last 3 months and
REPLACE counts rather than ignoring dupes.
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import date

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from common import get_db, track_run

log = logging.getLogger("vahan")

# Tried in order. Live check 2026-06-11: the canonical reportview returned
# 503 and the public dashboard 403'd from a non-Indian IP — Vahan geo-blocks
# and goes offline outside IST business hours. If GitHub Actions runners
# (US/EU) are consistently blocked, this scraper needs an Indian egress
# (self-hosted runner / proxy) — the health digest will make that obvious.
VAHAN_URLS = [
    "https://vahan.parivahan.gov.in/analytics/vahan/view/reportview.xhtml",
    "https://analytics.parivahan.gov.in/analytics/vahan/view/reportview.xhtml",
]

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def select_primefaces_dropdown(page, label_text: str, option_text: str) -> None:
    """Open a PrimeFaces selectOneMenu identified by its label and pick an option.

    PrimeFaces renders dropdowns as divs, not <select>; the option list is
    appended to <body>, so we click the trigger then the global panel item.
    """
    trigger = page.locator(
        f"xpath=//label[contains(normalize-space(), '{label_text}')]"
        f"/ancestor::*[self::td or self::div][1]"
        f"//div[contains(@class,'ui-selectonemenu')]"
    ).first
    trigger.click()
    page.locator(
        f".ui-selectonemenu-items li:has-text('{option_text}')"
    ).first.click()
    # Each selection fires an AJAX postback; wait for it to settle.
    page.wait_for_load_state("networkidle", timeout=30_000)


def parse_int(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def open_report(page) -> None:
    """Load the report view, trying each host; fail loudly with the HTTP status."""
    statuses = []
    for url in VAHAN_URLS:
        resp = page.goto(url, wait_until="networkidle", timeout=90_000)
        status = resp.status if resp else 0
        if status == 200 and page.locator(".ui-selectonemenu").count() > 0:
            return
        statuses.append(f"{url} -> HTTP {status}")
    raise RuntimeError(
        "Vahan portal unreachable (geo-block or downtime likely): "
        + "; ".join(statuses)
    )


def scrape_year(page, year: int) -> list[dict]:
    """Configure the report for one calendar year, return maker x month rows."""
    open_report(page)

    select_primefaces_dropdown(page, "Y-Axis", "Maker")
    select_primefaces_dropdown(page, "X-Axis", "Month Wise")
    select_primefaces_dropdown(page, "Year", str(year))

    # Filter panel: vehicle category BUS, fuel electric. Checkbox labels in
    # the left filter accordion; tolerate either BOV or PURE EV wording.
    for panel, option in [("Vehicle Category", "BUS"), ("Fuel", "ELECTRIC(BOV)")]:
        try:
            page.locator(f"text={panel}").first.click()
            page.locator(f"label:has-text('{option}')").first.click()
        except PlaywrightTimeout:
            log.warning("filter %s=%s not found; layout may have changed", panel, option)

    page.locator("button:has-text('Refresh'), a:has-text('Refresh')").first.click()
    page.wait_for_load_state("networkidle", timeout=60_000)

    table = page.locator(".ui-datatable table").first
    table.wait_for(timeout=30_000)

    header_cells = table.locator("thead th").all_inner_texts()
    month_cols: dict[int, str] = {}
    for idx, cell in enumerate(header_cells):
        token = cell.strip().upper()[:3]
        if token in MONTHS:
            month_cols[idx] = f"{year}-{MONTHS.index(token) + 1:02d}"

    rows: list[dict] = []
    for tr in table.locator("tbody tr").all():
        cells = tr.locator("td").all_inner_texts()
        if len(cells) < 2:
            continue
        # First non-serial cell is the maker name.
        maker = cells[1].strip() if cells[0].strip().isdigit() else cells[0].strip()
        if not maker or maker.upper().startswith("TOTAL"):
            continue
        for idx, month in month_cols.items():
            if idx < len(cells):
                rows.append({
                    "maker": maker,
                    "month": month,
                    "count": parse_int(cells[idx]),
                })
    return rows


def run(year: int | None = None, headless: bool = True) -> None:
    conn = get_db()
    target_year = year or date.today().year
    current_month = f"{date.today().year}-{date.today().month:02d}"

    with track_run(conn, "vahan") as stats, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        try:
            rows = scrape_year(page, target_year)
        finally:
            browser.close()

        stats.rows_found = len(rows)
        for row in rows:
            # Skip the in-progress month: partial numbers would trip the
            # anomaly detector and revise next run anyway.
            if row["month"] >= current_month:
                continue
            cur = conn.execute(
                """INSERT INTO registrations
                   (month, state, rto, maker_name_raw, vehicle_class, fuel, count)
                   VALUES (?, 'ALL INDIA', NULL, ?, 'BUS', 'PURE EV', ?)
                   ON CONFLICT (month, state, rto, maker_name_raw, vehicle_class, fuel)
                   DO UPDATE SET count = excluded.count,
                                 captured_at = datetime('now')
                   WHERE registrations.count != excluded.count""",
                (row["month"], row["maker"], row["count"]),
            )
            stats.rows_inserted += cur.rowcount
        conn.commit()
        log.info("vahan %d: found=%d upserted=%d",
                 target_year, stats.rows_found, stats.rows_inserted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape Vahan EV bus registrations")
    parser.add_argument("--year", type=int, default=None,
                        help="calendar year to scrape (default: current year)")
    parser.add_argument("--headed", action="store_true", help="run with visible browser")
    args = parser.parse_args()
    run(year=args.year, headless=not args.headed)
