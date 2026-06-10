"""Daily health-check digest.

Reads scrape_runs and the data tables, flags problems, and emits a markdown
digest to stdout (GitHub Actions pipes it into the job summary). Exits 1 if
any CRITICAL flag fired so the workflow can open/ping a GitHub issue.

Checks:
  1. Missing runs    — a scraper produced no scrape_runs row in 36h.
  2. Errors          — latest run of any scraper has status='error'.
  3. Zero-row capture— status='empty' (source up but yielded nothing).
  4. Insert drought  — N consecutive runs with rows_inserted=0 for sources
                       that should produce something weekly (cesl excluded:
                       new tenders are genuinely sparse).
  5. Volume anomaly  — latest completed Vahan month deviates >60% from the
                       trailing 6-month median (catches silent mis-parses).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from statistics import median

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from common import get_db  # noqa: E402

EXPECTED_SCRAPERS = ["bse", "cesl", "vahan"]
DROUGHT_RUNS = 7            # consecutive zero-insert runs before flagging
ANOMALY_THRESHOLD = 0.60    # fraction deviation from trailing median
STALE_HOURS = 36


def check_runs(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    critical, warnings = [], []
    for scraper in EXPECTED_SCRAPERS:
        latest = conn.execute(
            """SELECT * FROM scrape_runs WHERE scraper = ?
               ORDER BY started_at DESC LIMIT 1""",
            (scraper,),
        ).fetchone()
        if latest is None:
            critical.append(f"**{scraper}**: has never run.")
            continue
        age_hours = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 24", (latest["started_at"],)
        ).fetchone()[0]
        if age_hours > STALE_HOURS:
            critical.append(
                f"**{scraper}**: no run in {age_hours:.0f}h (cron broken?)."
            )
        if latest["status"] == "error":
            critical.append(f"**{scraper}**: last run errored — `{latest['error']}`")
        elif latest["status"] == "empty":
            critical.append(
                f"**{scraper}**: ZERO rows captured — source layout likely changed."
            )

        if scraper != "cesl":  # new CESL tenders are legitimately rare
            recent = conn.execute(
                """SELECT rows_inserted FROM scrape_runs
                   WHERE scraper = ? AND status IN ('ok','empty')
                   ORDER BY started_at DESC LIMIT ?""",
                (scraper, DROUGHT_RUNS),
            ).fetchall()
            if len(recent) == DROUGHT_RUNS and all(r[0] == 0 for r in recent):
                warnings.append(
                    f"**{scraper}**: {DROUGHT_RUNS} consecutive runs with no new "
                    f"rows — dedupe bug or stale source?"
                )
    return critical, warnings


def check_vahan_anomaly(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT month, SUM(count) AS total FROM registrations
           GROUP BY month ORDER BY month DESC LIMIT 7"""
    ).fetchall()
    if len(rows) < 4:
        return []  # not enough history to judge
    latest, history = rows[0], [r["total"] for r in rows[1:]]
    base = median(history)
    if base == 0:
        return []
    deviation = abs(latest["total"] - base) / base
    if deviation > ANOMALY_THRESHOLD:
        return [
            f"**vahan**: {latest['month']} total ({latest['total']}) deviates "
            f"{deviation:.0%} from trailing median ({base:.0f}) — verify parse "
            f"before trusting (could also be a real FAME/PM-eBus delivery spike)."
        ]
    return []


def table_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    tables = ["organizations", "tenders", "tender_events", "bids",
              "deployments", "registrations", "charging_events", "announcements"]
    return [
        (t, conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # noqa: S608 — table names are a fixed allowlist
        for t in tables
    ]


def main() -> int:
    conn = get_db()
    critical, warnings = check_runs(conn)
    warnings += check_vahan_anomaly(conn)

    untriaged = conn.execute(
        "SELECT COUNT(*) FROM announcements WHERE triaged = 0"
    ).fetchone()[0]

    print("# 🚌 ev-bus-tracker daily health digest\n")
    status = "🔴 CRITICAL" if critical else ("🟡 warnings" if warnings else "🟢 healthy")
    print(f"**Status: {status}**\n")
    if critical:
        print("## Critical\n")
        for item in critical:
            print(f"- {item}")
        print()
    if warnings:
        print("## Warnings\n")
        for item in warnings:
            print(f"- {item}")
        print()
    if untriaged:
        print(f"## Triage queue\n\n- {untriaged} BSE announcement(s) awaiting "
              f"classification into tenders/deployments.\n")
    print("## Row counts\n")
    print("| table | rows |\n|---|---|")
    for table, count in table_counts(conn):
        print(f"| {table} | {count} |")

    return 1 if critical else 0


if __name__ == "__main__":
    sys.exit(main())
