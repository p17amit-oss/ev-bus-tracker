#!/bin/zsh
# Runs the Vahan scraper locally (needs Indian IP) and pushes the DB update.
# Scheduled via launchd — see setup instructions in README.
set -e
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null || true
python scrapers/vahan.py --year "$(date +%Y)"
python pipeline/export_json.py
git add data/evbus.db site/src/data/registrations_monthly.json
git diff --cached --quiet || git commit -m "data: vahan update $(date -u +%F)"
git push
