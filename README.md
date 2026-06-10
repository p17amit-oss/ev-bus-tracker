# ev-bus-tracker

Electric bus market intelligence for India: tenders, OEM order disclosures,
deployments, and monthly registrations — scraped daily, stored in SQLite,
published as a static site.

## Architecture (zero-DevOps, ~₹0/month infra)

```
GitHub Actions cron (daily, 08:00 IST)
  ├─ scrapers/bse.py     → announcements        (requests, JSON API)
  ├─ scrapers/cesl.py    → tenders + events     (Playwright)
  ├─ scrapers/vahan.py   → registrations        (Playwright)
  ├─ data/evbus.db       committed back to repo (data-in-git)
  ├─ pipeline/health_check.py → job summary + GitHub issue on red
  └─ pipeline/export_json.py  → site/src/data/*.json
Cloudflare Pages (free tier) builds site/ (Astro, fully static) on push.
```

Costs: GitHub Actions free tier (~15 min/day used of 2,000 min/month),
Cloudflare Pages free tier, no servers, no database hosting. Only real cost
is a domain (~₹800/yr).

## Repo layout

```
db/schema.sql          # the single source of truth for the data model
scrapers/              # one file per source + common.py plumbing
pipeline/              # health check digest, JSON export for the site
config/                # watched companies, keywords
data/evbus.db          # the SQLite database (committed)
site/                  # Astro static site (Cloudflare Pages)
.github/workflows/     # daily cron
```

## Local setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # check pkgs against approved list first
playwright install chromium
python scrapers/bse.py --days-back 90  # backfill
python scrapers/cesl.py
python scrapers/vahan.py --year 2026
python pipeline/health_check.py
python pipeline/export_json.py
cd site && npm install && npm run dev
```

## Operating notes

- **Scrape politely.** Low volume, daily cadence, honest user agent where
  possible. Respect each source's terms; Vahan and BSE data are public
  records but their portals are shared infrastructure.
- **Dedupe keys everywhere.** Re-running any scraper is always safe.
- **BSE announcements land in a staging table** (`announcements`) and are
  triaged into `tenders` / `deployments` as a second step — a parsing bug
  can never corrupt curated tables.
- **Vahan numbers revise backwards** (states upload late); the scraper
  re-captures the trailing months and updates counts in place.
- **When a scraper breaks** (it will — selectors rot), the health digest
  opens a GitHub issue. Fix the selector, re-run via workflow_dispatch.

## Newsletter (next phase)

Weekly digest generated from `tender_events` + `announcements` deltas,
sent via Buttondown/Listmonk — not built yet.
