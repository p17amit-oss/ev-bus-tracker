"""BSE corporate announcements scraper.

Pulls recent announcements for each watched scrip code from BSE's public
announcement JSON API, keyword-filters for e-bus signals, and stages matches
in the `announcements` table for triage. Triage into tenders/deployments is
deliberately a separate step so a bad parse can never corrupt curated tables.

API notes (validated live against api.bseindia.com on 2026-06-11):
  GET https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w
  params: pageno, strCat=-1, strPrevDate, strToDate (YYYYMMDD),
          strScrip, strSearch=P, strType=C, subcategory=-1
  Pages hold 50 rows; Table1[0].ROWCNT is the total row count.
  When the window is empty the API returns the JSON *string*
  "No Record Found!" instead of an object.
  Requires a browser-ish UA and a Referer of bseindia.com or it 403s.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, timedelta

from common import dedupe_key, get_db, http_session, load_config, track_run, upsert_org

log = logging.getLogger("bse")

API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

HEADERS = {
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
}

REQUEST_GAP_SECONDS = 2.0  # be polite; we are a guest on their API
PAGE_SIZE = 50
MAX_PAGES_PER_SCRIP = 5


def fetch_announcements(session, scrip_code: str, days_back: int) -> list[dict]:
    """Fetch announcement rows for one scrip over the lookback window."""
    today = date.today()
    params_base = {
        "pageno": 1,
        "strCat": "-1",
        "strPrevDate": (today - timedelta(days=days_back)).strftime("%Y%m%d"),
        "strToDate": today.strftime("%Y%m%d"),
        "strScrip": scrip_code,
        "strSearch": "P",
        "strType": "C",
        "subcategory": "-1",
    }
    rows: list[dict] = []
    for page in range(1, MAX_PAGES_PER_SCRIP + 1):
        params = {**params_base, "pageno": page}
        resp = session.get(API_URL, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            # BSE serves HTML error pages with a 200 when it rate-limits.
            raise RuntimeError(
                f"BSE returned non-JSON for scrip {scrip_code} page {page}: "
                f"{resp.text[:200]!r}"
            ) from exc
        if isinstance(payload, str):
            # "No Record Found!" — empty window, not an error.
            break
        table = payload.get("Table") or []
        rows.extend(table)
        rowcnt = (payload.get("Table1") or [{}])[0].get("ROWCNT") or 0
        if page * PAGE_SIZE >= rowcnt or not table:
            break
        time.sleep(REQUEST_GAP_SECONDS)
    return rows


def matched_keywords(headline: str, keywords: list[str]) -> list[str]:
    hay = headline.lower()
    return [kw for kw in keywords if kw in hay]


def run(days_back: int = 3) -> None:
    conn = get_db()
    cfg = load_config("watched_companies.json")
    keywords = [kw.lower() for kw in cfg["keywords"]]
    session = http_session()
    # The API rejects non-browser UAs; present a realistic one. We still
    # identify in the Referer-less rate limits by keeping volume tiny.
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )

    with track_run(conn, "bse") as stats:
        for company in cfg["companies"]:
            scrip = company.get("scrip_code")
            if not scrip:
                continue
            org_id = upsert_org(
                conn, company["name"], company["slug"], company["org_type"],
                bse_scrip_code=scrip,
            )
            try:
                rows = fetch_announcements(session, scrip, days_back)
            except Exception as exc:  # one bad scrip must not kill the run
                log.warning("scrip %s failed: %s", scrip, exc)
                stats.warnings.append(f"{company['slug']}: {exc}")
                continue
            stats.rows_found += len(rows)
            for row in rows:
                headline = (row.get("HEADLINE") or row.get("NEWSSUB") or "").strip()
                if not headline:
                    continue
                # Match against every text field BSE exposes — headlines are
                # often boilerplate while the substance sits in NEWSSUB.
                searchable = " ".join(
                    str(row.get(f) or "")
                    for f in ("HEADLINE", "NEWSSUB", "SUBCATNAME", "MORE")
                )
                hits = matched_keywords(searchable, keywords)
                if not hits:
                    continue
                news_id = str(row.get("NEWSID") or "")
                attachment = (row.get("ATTACHMENTNAME") or "").strip()
                pdf_url = ATTACH_BASE + attachment if attachment else None
                key = dedupe_key("bse", news_id or headline, scrip)
                cur = conn.execute(
                    """INSERT OR IGNORE INTO announcements
                       (source, org_id, scrip_code, headline, category,
                        announced_at, pdf_url, matched_terms, dedupe_key, raw_json)
                       VALUES ('bse', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        org_id, scrip, headline,
                        row.get("CATEGORYNAME"),
                        row.get("NEWS_DT") or row.get("DissemDT"),
                        pdf_url,
                        json.dumps(hits),
                        key,
                        json.dumps(row, ensure_ascii=False),
                    ),
                )
                stats.rows_inserted += cur.rowcount
            conn.commit()
            time.sleep(REQUEST_GAP_SECONDS)
        log.info("bse: found=%d inserted=%d", stats.rows_found, stats.rows_inserted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape BSE announcements")
    parser.add_argument("--days-back", type=int, default=3,
                        help="lookback window in days (default 3; use 90 for backfill)")
    args = parser.parse_args()
    run(days_back=args.days_back)
