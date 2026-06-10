"""BSE announcement triage pipeline.

Reads un-triaged rows from `announcements`, classifies each one, and writes
results into `deployments` or `tender_events` as appropriate. Marks every
processed row as triaged=1 whether or not it produced a record (so junk
filings don't re-appear in the queue).

Classification rules (in priority order):
  1. SEBI/regulatory boilerplate  → mark triaged, no record created
  2. Order / LOA keyword match    → deployments row
  3. Market share / press release → deployment note (informational)
  4. Tender / bid mention         → tender_event row (if a parent tender exists)
  5. Fallback                     → mark triaged, logged as 'unclassified'

The rules are intentionally conservative. It is better to miss a record than
to create a wrong one. Un-matched genuine announcements surface in the health
digest triage queue so a human can handle them.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from common import dedupe_key, get_db  # noqa: E402

log = logging.getLogger("triage")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# ── Regex patterns ────────────────────────────────────────────────────────────

# Boilerplate / non-order filings — skip these immediately.
BOILERPLATE_RE = re.compile(
    r"large corporate|sebi circular|credit rating|borrowing framework|"
    r"initial disclosure|annexure[- ]a|company secretary|cin no\.|"
    r"financial result|annual report|agm|board meeting|dividend|"
    r"investor presentation|audio recording|transcript",
    re.IGNORECASE,
)

# Strong order signals.
ORDER_RE = re.compile(
    r"\b(letter of award|letter of acceptance|loa|bags? order|bagging|"
    r"supply order|purchase order|work order|receives? order|"
    r"order received|order win|wins? order|secured? order)\b",
    re.IGNORECASE,
)

# Bus count in the text  e.g. "500 electric buses", "1,200 e-buses"
COUNT_RE = re.compile(
    r"\b([\d,]{2,7})\s*(?:nos?\.?\s*)?(?:electric\s+)?(?:e[- ]?)?bus(?:es)?\b",
    re.IGNORECASE,
)

# City names to extract deployment location.
CITY_RE = re.compile(
    r"\b(Mumbai|Delhi|Bangalore|Bengaluru|Chennai|Hyderabad|Pune|Kolkata|"
    r"Ahmedabad|Surat|Jaipur|Lucknow|Chandigarh|Bhopal|Indore|Nagpur|"
    r"Kochi|Thiruvananthapuram|Vizag|Visakhapatnam|Coimbatore|Vadodara|"
    r"Agra|Varanasi|Patna|Bhubaneswar|Guwahati|Dehradun|Raipur|Ranchi)\b",
    re.IGNORECASE,
)

STATE_RE = re.compile(
    r"\b(Maharashtra|Delhi|Karnataka|Tamil Nadu|Telangana|Andhra Pradesh|"
    r"Gujarat|Rajasthan|Uttar Pradesh|Punjab|Haryana|Madhya Pradesh|"
    r"West Bengal|Kerala|Odisha|Bihar|Assam|Jharkhand|Uttarakhand|"
    r"Chhattisgarh|Goa)\b",
    re.IGNORECASE,
)

TENDER_RE = re.compile(
    r"\b(tender|rfp|bid|cesl|gcc|pm[- ]?e[- ]?bus|pm[- ]?ebus|fame)\b",
    re.IGNORECASE,
)

PRESS_RELEASE_RE = re.compile(
    r"\b(market share|press release|dispatch|delivery|delivered|fleet|"
    r"commission|inaugurate|launch)\b",
    re.IGNORECASE,
)


def clean_html(text: str) -> str:
    """Strip HTML tags; decode common entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return " ".join(text.split())


def extract_count(text: str) -> int | None:
    m = COUNT_RE.search(text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def classify(headline: str, more: str, matched_terms: list[str]) -> str:
    """Return one of: boilerplate | order | press_release | tender | unclassified"""
    combined = f"{headline} {more}"
    if BOILERPLATE_RE.search(combined):
        return "boilerplate"
    if ORDER_RE.search(combined):
        return "order"
    if TENDER_RE.search(combined):
        return "tender"
    if PRESS_RELEASE_RE.search(combined):
        return "press_release"
    return "unclassified"


def triage_announcement(conn, row) -> str:
    """Process one announcement row. Returns the classification label."""
    raw = json.loads(row["raw_json"] or "{}")
    headline = clean_html(row["headline"] or "")
    more = clean_html(raw.get("MORE") or "")
    matched = json.loads(row["matched_terms"] or "[]")
    combined = f"{headline} {more}"

    label = classify(headline, more, matched)

    if label == "order":
        bus_count = extract_count(combined)
        city_m = CITY_RE.search(combined)
        state_m = STATE_RE.search(combined)
        city = city_m.group(1).title() if city_m else None
        state = state_m.group(1).title() if state_m else None
        key = dedupe_key("deployment-bse", row["dedupe_key"])
        conn.execute(
            """INSERT OR IGNORE INTO deployments
               (operator_org_id, oem_org_id, bus_count, city, state,
                status, source_url, notes, created_at)
               VALUES (NULL, ?, ?, ?, ?, 'announced', ?, ?, datetime('now'))""",
            (
                row["org_id"],
                bus_count,
                city,
                state,
                row["pdf_url"] or "",
                f"From BSE announcement: {headline[:200]}",
            ),
        )
        log.info("order → deployment: %s buses in %s", bus_count, city or state or "?")

    elif label == "tender":
        # Link to an existing tender if we can match by issuer
        existing = conn.execute(
            "SELECT id FROM tenders WHERE issuing_org_id = ? LIMIT 1",
            (row["org_id"],),
        ).fetchone()
        if existing:
            key = dedupe_key("tender-event-bse", row["dedupe_key"])
            conn.execute(
                """INSERT OR IGNORE INTO tender_events
                   (tender_id, event_type, event_date, details, source_url, dedupe_key)
                   VALUES (?, 'other', ?, ?, ?, ?)""",
                (
                    existing["id"],
                    row["announced_at"],
                    headline[:300],
                    row["pdf_url"] or "",
                    key,
                ),
            )
            log.info("tender event linked to tender %d", existing["id"])

    elif label == "press_release":
        # Informational only — record as a deployment note if bus count found
        bus_count = extract_count(combined)
        if bus_count:
            city_m = CITY_RE.search(combined)
            state_m = STATE_RE.search(combined)
            conn.execute(
                """INSERT OR IGNORE INTO deployments
                   (oem_org_id, bus_count, city, state, status, source_url, notes, created_at)
                   VALUES (?, ?, ?, ?, 'active', ?, ?, datetime('now'))""",
                (
                    row["org_id"],
                    bus_count,
                    city_m.group(1).title() if city_m else None,
                    state_m.group(1).title() if state_m else None,
                    row["pdf_url"] or "",
                    f"Press release: {headline[:200]}",
                ),
            )
            log.info("press_release → deployment note: %s buses", bus_count)

    log.info("ann #%d [%s] → %s", row["id"], (row["scrip_code"] or ""), label)
    return label


def run() -> None:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM announcements WHERE triaged = 0 ORDER BY announced_at"
    ).fetchall()

    if not rows:
        log.info("triage: nothing to process")
        return

    counts: dict[str, int] = {}
    for row in rows:
        label = triage_announcement(conn, row)
        counts[label] = counts.get(label, 0) + 1
        conn.execute(
            "UPDATE announcements SET triaged = 1 WHERE id = ?", (row["id"],)
        )
    conn.commit()

    log.info(
        "triage complete: %d processed — %s",
        len(rows),
        ", ".join(f"{v} {k}" for k, v in sorted(counts.items())),
    )


if __name__ == "__main__":
    run()
