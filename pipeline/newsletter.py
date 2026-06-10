"""Weekly newsletter digest generator + Buttondown sender.

Generates a "India EV Bus Digest" covering the past 7 days:
  - New tenders / deadline reminders
  - BSE order disclosures
  - New deployments
  - Vahan registration snapshot

Sends via Buttondown API (free up to 100 subscribers).
Set BUTTONDOWN_API_KEY in GitHub Actions secrets (or .env locally).

Run manually:   python pipeline/newsletter.py --dry-run   (prints to stdout)
Run to send:    python pipeline/newsletter.py --send
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from common import get_db  # noqa: E402

log = logging.getLogger("newsletter")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

SITE_URL = "https://ev-bus-tracker.pages.dev"
BUTTONDOWN_API = "https://api.buttondown.email/v1/emails"


# ── Data fetchers ─────────────────────────────────────────────────────────────

def new_tenders(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.*, o.name AS issuer_name
           FROM tenders t LEFT JOIN organizations o ON o.id = t.issuing_org_id
           WHERE t.created_at >= ? AND t.status IN ('open','announced','extended')
           ORDER BY t.bus_count DESC NULLS LAST""",
        (since,),
    ).fetchall()


def closing_soon(conn: sqlite3.Connection, within_days: int = 14) -> list[sqlite3.Row]:
    today = date.today().isoformat()
    deadline = (date.today() + timedelta(days=within_days)).isoformat()
    return conn.execute(
        """SELECT t.*, o.name AS issuer_name
           FROM tenders t LEFT JOIN organizations o ON o.id = t.issuing_org_id
           WHERE t.bid_due_date BETWEEN ? AND ?
             AND t.status IN ('open','extended')
           ORDER BY t.bid_due_date""",
        (today, deadline),
    ).fetchall()


def new_announcements(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    # Exclude boilerplate SEBI filings — their headlines contain HTML tags
    # or the word "Large Corporate" which marks them as regulatory noise.
    return conn.execute(
        """SELECT a.*, o.name AS org_name
           FROM announcements a LEFT JOIN organizations o ON o.id = a.org_id
           WHERE a.created_at >= ? AND a.triaged = 1
             AND a.matched_terms != '[]'
             AND a.headline NOT LIKE '%Large Corporate%'
             AND a.headline NOT LIKE '%<b>%'
             AND a.headline NOT LIKE '%Format of Initial%'
           ORDER BY a.announced_at DESC""",
        (since,),
    ).fetchall()


def new_deployments(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT d.*, oe.name AS oem_name, op.name AS operator_name
           FROM deployments d
           LEFT JOIN organizations oe ON oe.id = d.oem_org_id
           LEFT JOIN organizations op ON op.id = d.operator_org_id
           WHERE d.created_at >= ?
           ORDER BY d.bus_count DESC NULLS LAST""",
        (since,),
    ).fetchall()


def registration_snapshot(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    latest_month = conn.execute(
        "SELECT MAX(month) FROM registrations"
    ).fetchone()[0]
    if not latest_month:
        return []
    return conn.execute(
        """SELECT maker_name_raw AS maker, SUM(count) AS count
           FROM registrations WHERE month = ?
           GROUP BY maker_name_raw ORDER BY count DESC LIMIT 8""",
        (latest_month,),
    ).fetchall()


# ── Markdown builder ──────────────────────────────────────────────────────────

def build_markdown(conn: sqlite3.Connection, since: str) -> tuple[str, str]:
    """Return (subject, markdown_body)."""
    today = date.today()
    week_label = today.strftime("%d %b %Y")

    new_t    = new_tenders(conn, since)
    closing  = closing_soon(conn)
    ann      = new_announcements(conn, since)
    new_dep  = new_deployments(conn, since)
    reg      = registration_snapshot(conn)

    has_content = any([new_t, closing, ann, new_dep, reg])

    subject = f"India EV Bus Digest — {week_label}"
    if closing:
        subject = f"⚡ {closing[0]['bus_count']:,} buses due {closing[0]['bid_due_date']} · India EV Bus Digest {week_label}"

    lines: list[str] = []

    lines.append(f"# India EV Bus Digest · {week_label}\n")
    lines.append(
        f"Your weekly summary of India's electric bus market — "
        f"tenders, orders, and registrations.\n"
    )
    lines.append(f"[View live tracker →]({SITE_URL})\n\n---\n")

    # ── Deadlines ──────────────────────────────────────────────────────────
    if closing:
        lines.append("## ⏰ Bid Deadlines This Fortnight\n")
        for t in closing:
            buses = f"{t['bus_count']:,} buses · " if t['bus_count'] else ""
            issuer = t['issuer_name'] or "Unknown"
            import re as _re
            title = _re.sub(r'^\d+\s+', '', (t['title'] or '')).strip()[:100]
            lines.append(
                f"- **{t['bid_due_date']}** — [{title}]({t['source_url'] or '#'})  \n"
                f"  {buses}{issuer}"
                + (f" · {t['procurement_model'].upper()}" if t['procurement_model'] not in ('unknown', None) else "")
                + "\n"
            )
        lines.append("\n")

    # ── New tenders ────────────────────────────────────────────────────────
    if new_t:
        lines.append("## 📋 New Tenders\n")
        for t in new_t:
            buses = f"**{t['bus_count']:,} buses**" if t['bus_count'] else "buses TBD"
            issuer = t['issuer_name'] or "Unknown"
            import re as _re
            title = _re.sub(r'^\d+\s+', '', (t['title'] or '')).strip()[:120]
            lines.append(f"- [{title}]({t['source_url'] or '#'}) — {buses} · {issuer}\n")
        lines.append("\n")

    # ── BSE disclosures ────────────────────────────────────────────────────
    if ann:
        lines.append("## 📢 Corporate Disclosures\n")
        for a in ann[:8]:
            company = a['org_name'] or f"Scrip {a['scrip_code']}"
            headline = (a['headline'] or '').strip()[:120]
            date_str = (a['announced_at'] or '')[:10]
            pdf = f" · [PDF]({a['pdf_url']})" if a['pdf_url'] else ""
            lines.append(f"- **{company}** ({date_str}) — {headline}{pdf}\n")
        lines.append("\n")

    # ── New deployments ────────────────────────────────────────────────────
    if new_dep:
        lines.append("## 🚌 New Deployments\n")
        for d in new_dep[:8]:
            oem = d['oem_name'] or '?'
            op  = d['operator_name'] or '?'
            loc = ', '.join(filter(None, [d['city'], d['state']])) or 'India'
            buses = d['bus_count'] or '?'
            lines.append(f"- **{oem}** → {op} · {buses} buses · {loc}\n")
        lines.append("\n")

    # ── Registration snapshot ──────────────────────────────────────────────
    if reg:
        month = conn.execute("SELECT MAX(month) FROM registrations").fetchone()[0]
        total = sum(r['count'] for r in reg)
        lines.append(f"## 📊 Vahan Registrations — {month}\n")
        lines.append(f"Total e-buses registered: **{total:,}**\n\n")
        lines.append("| Maker | Units |\n|---|---|\n")
        for r in reg:
            lines.append(f"| {r['maker']} | {r['count']:,} |\n")
        lines.append("\n")

    if not has_content:
        lines.append(
            "_No new data this week — scrapers may be catching up or "
            "sources had no updates. Check the [health digest]"
            f"({SITE_URL}) for details._\n"
        )

    lines.append("---\n")
    lines.append(
        f"[Unsubscribe]({{{{ unsubscribe_url }}}}) · "
        f"[View online]({{{{ email_url }}}}) · "
        f"[EV Bus Tracker]({SITE_URL})\n"
    )

    return subject, "".join(lines)


# ── Sender ────────────────────────────────────────────────────────────────────

def send_via_buttondown(subject: str, body: str, api_key: str) -> None:
    import urllib.request

    payload = json.dumps({
        "subject": subject,
        "body": body,
        "status": "scheduled",   # change to "draft" to review before sending
    }).encode("utf-8")

    req = urllib.request.Request(
        BUTTONDOWN_API,
        data=payload,
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
        log.info("Buttondown email created: id=%s status=%s", result.get("id"), result.get("status"))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and send weekly EV bus digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print digest to stdout, do not send")
    parser.add_argument("--send", action="store_true",
                        help="Send via Buttondown (requires BUTTONDOWN_API_KEY env var)")
    parser.add_argument("--days-back", type=int, default=7,
                        help="Lookback window in days (default 7)")
    args = parser.parse_args()

    since = (date.today() - timedelta(days=args.days_back)).isoformat()
    conn = get_db()
    subject, body = build_markdown(conn, since)

    if args.dry_run or not args.send:
        print(f"SUBJECT: {subject}\n{'─'*60}\n{body}")
        return

    api_key = os.environ.get("BUTTONDOWN_API_KEY")
    if not api_key:
        log.error("BUTTONDOWN_API_KEY not set — export it or use --dry-run")
        sys.exit(1)

    send_via_buttondown(subject, body, api_key)
    log.info("Newsletter sent: %s", subject)


if __name__ == "__main__":
    main()
