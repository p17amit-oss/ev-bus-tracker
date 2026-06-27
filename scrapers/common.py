"""Shared plumbing for all scrapers: DB access, run tracking, HTTP session.

Every scraper wraps its work in `track_run(...)` so the health-check digest
can see exactly what happened on every execution, including crashes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "evbus.db"
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"
SEED_PATH = REPO_ROOT / "db" / "seed.sql"

# Honest, contactable UA. Scraping public data politely: identify yourself,
# keep request rates low, and respect robots/ToS of each source.
USER_AGENT = (
    "evbus-tracker/0.1 (+https://github.com/PLACEHOLDER/ev-bus-tracker; "
    "research aggregator; contact: set-me-in-config)"
)

REQUEST_TIMEOUT = 30  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)


def get_db() -> sqlite3.Connection:
    """Open the SQLite DB, creating it from schema.sql on first run.

    Both scripts are idempotent: schema.sql is all CREATE ... IF NOT EXISTS,
    and seed.sql is all INSERT OR IGNORE on unique keys. So this is safe to run
    on every connection — it builds a fresh DB fully (schema + reference data)
    and is a no-op against the already-populated live DB.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())  # idempotent (IF NOT EXISTS)
    conn.executescript(SEED_PATH.read_text())    # idempotent (INSERT OR IGNORE)
    return conn


def dedupe_key(*parts: str) -> str:
    """Stable hash for natural-key dedup across re-scrapes."""
    normalized = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def http_session():
    # Imported lazily so DB-only consumers (health check, site export)
    # don't need requests installed.
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


@dataclass
class RunStats:
    """Mutable counters a scraper fills in while it works."""

    rows_found: int = 0
    rows_inserted: int = 0
    warnings: list[str] = field(default_factory=list)


@contextmanager
def track_run(conn: sqlite3.Connection, scraper: str, source_key: str):
    """Record a scrape_runs row around a scraper execution.

    source_key is passed explicitly (not derived from `scraper`) so the link to
    source_coverage is intentional. It is written on INSERT and re-asserted on
    the status UPDATE so the trg_scrape_run_update_coverage trigger — which
    fires AFTER UPDATE OF status and requires NEW.source_key IS NOT NULL — sees
    a non-NULL key and refreshes source_coverage.last_crawled_at / crawl_status.

    Status semantics:
      ok    -> ran fine and saw rows at the source
      empty -> ran fine but the source yielded zero rows (digest flags this)
      error -> raised; the exception is stored and re-raised so CI goes red
    """
    cur = conn.execute(
        "INSERT INTO scrape_runs (scraper, source_key, started_at) VALUES (?, ?, datetime('now'))",
        (scraper, source_key),
    )
    run_id = cur.lastrowid
    conn.commit()
    stats = RunStats()
    try:
        yield stats
    except Exception as exc:
        conn.execute(
            """UPDATE scrape_runs
               SET finished_at = datetime('now'), status = 'error',
                   source_key = ?, rows_found = ?, rows_inserted = ?, error = ?
               WHERE id = ?""",
            (source_key, stats.rows_found, stats.rows_inserted, repr(exc)[:2000], run_id),
        )
        conn.commit()
        raise
    status = "ok" if stats.rows_found > 0 else "empty"
    error = "; ".join(stats.warnings)[:2000] or None
    conn.execute(
        """UPDATE scrape_runs
           SET finished_at = datetime('now'), status = ?,
               source_key = ?, rows_found = ?, rows_inserted = ?, error = ?
           WHERE id = ?""",
        (status, source_key, stats.rows_found, stats.rows_inserted, error, run_id),
    )
    conn.commit()


def load_config(name: str) -> dict:
    """Load a JSON config file from config/."""
    path = REPO_ROOT / "config" / name
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def upsert_org(conn: sqlite3.Connection, name: str, slug: str, org_type: str,
               **fields) -> int:
    """Insert an organization if its slug is new; return its id either way."""
    row = conn.execute(
        "SELECT id FROM organizations WHERE slug = ?", (slug,)
    ).fetchone()
    if row:
        return row["id"]
    cols = ["name", "slug", "org_type", *fields.keys()]
    placeholders = ", ".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO organizations ({', '.join(cols)}) VALUES ({placeholders})",
        (name, slug, org_type, *fields.values()),
    )
    return cur.lastrowid
