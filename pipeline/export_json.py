"""Export the SQLite DB to JSON files the Astro site builds from.

Keeps the site build dependency-free on the Python side: Cloudflare Pages
just runs `astro build` against committed JSON. Run after scrapers, or let
the GitHub Action do it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from common import get_db  # noqa: E402

OUT_DIR = REPO_ROOT / "site" / "src" / "data"


def rows(conn, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def main() -> None:
    conn = get_db()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    exports = {
        "tenders.json": rows(conn, """
            SELECT t.*, o.name AS issuer_name, o.slug AS issuer_slug
            FROM tenders t LEFT JOIN organizations o ON o.id = t.issuing_org_id
            ORDER BY t.created_at DESC"""),
        "organizations.json": rows(conn, "SELECT * FROM organizations ORDER BY name"),
        "registrations_monthly.json": rows(conn, """
            SELECT month, maker_name_raw AS maker, SUM(count) AS count
            FROM registrations GROUP BY month, maker_name_raw
            ORDER BY month, count DESC"""),
        "deployments.json": rows(conn, """
            SELECT d.*, op.name AS operator_name, oe.name AS oem_name
            FROM deployments d
            LEFT JOIN organizations op ON op.id = d.operator_org_id
            LEFT JOIN organizations oe ON oe.id = d.oem_org_id
            ORDER BY d.deployment_date DESC"""),
    }
    for filename, data in exports.items():
        (OUT_DIR / filename).write_text(
            json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        print(f"wrote {filename}: {len(data)} rows")


if __name__ == "__main__":
    main()
