"""pipeline/group_tenders.py — detect and apply cross-source tender clusters.

Default mode (detection):
  Evaluates all cross-source tender pairs for four signals:
    1. scheme exact (both non-unknown, equal)
    2. city overlap (non-empty intersection of cities[] JSON arrays)
    3. bus_count within 2%
    4. bid_due_date within ±7 days (falls back to issue_date when bid_due_date absent)

  Outcomes per pair:
    all 4 signals + both single-city     → auto-group (new tender_groups row, or
                                            attach to existing if one member already
                                            has a group_id)
    exactly 3-of-4, both single-city     → grouping_suggestions row (status=pending)
    either tender is_multi_city=1        → deferred: skip if no lots; lot-level
                                            grouping runs once tender_lots populated
                                            from PDF extraction
    pair already has any suggestion row  → skipped (idempotent; rejected pairs are
                                            suppressed from re-proposal by this rule)

--apply mode:
  Reads grouping_suggestions with status='accepted'. For each: creates/attaches the
  group and sets group_id on both member tenders (only if group_id is currently NULL,
  never overwrites). Stamps reviewer/reviewed_at if not already set.

--fixture mode:
  Runs detection against an in-memory DB seeded with four synthetic cases. Does NOT
  open or touch data/evbus.db. Prints PASS/FAIL for each case and exits.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from common import get_db  # noqa: E402

log = logging.getLogger("group_tenders")

# Lower index = more authoritative. STU portals are first-party issuers;
# Vahan is a registration registry with no tender content.
SOURCE_AUTHORITY: dict[str, int] = {
    "dtc": 0, "best": 0, "bmtc": 0, "apsrtc": 0, "tsrtc": 0,
    "cesl": 1,
    "cppp": 2,
    "press": 3,
    "user_report": 4,
    "vahan": 5,
}

# Ordered from strongest to weakest confidence. Index is used for ranking.
CONFIDENCE_ORDER = ["confirmed", "reported", "estimated", "inferred"]

SCHEME_LABEL: dict[str, str] = {
    "pm_ebus_sewa": "PM e-Bus Sewa",
    "pm_edrive":    "PM E-DRIVE",
    "fame_2":       "FAME-II",
    "state_funded": "State-funded",
    "smart_city":   "Smart City",
    "other":        "Other",
}

DATE_WINDOW_DAYS = 7
MIN_SIGNALS_FOR_SUGGESTION = 3


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_cities(json_str: str | None) -> set[str]:
    """Return a lowercase set of city names from the tenders.cities JSON column."""
    if not json_str:
        return set()
    try:
        lst = json.loads(json_str)
        return {c.strip().lower() for c in lst if c and c.strip()}
    except (json.JSONDecodeError, TypeError):
        return set()


def source_priority(source_key: str | None) -> int:
    return SOURCE_AUTHORITY.get(source_key or "", 10)


def confidence_rank(conf: str | None) -> int:
    try:
        return CONFIDENCE_ORDER.index(conf or "inferred")
    except ValueError:
        return len(CONFIDENCE_ORDER) - 1


def lower_confidence(a: str | None, b: str | None) -> str:
    """Return the weaker (more conservative) of two confidence values."""
    worse = max(confidence_rank(a), confidence_rank(b))
    return CONFIDENCE_ORDER[min(worse, len(CONFIDENCE_ORDER) - 1)]


def has_lots(conn: sqlite3.Connection, tender_id: int) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM tender_lots WHERE tender_id = ?", (tender_id,)
    ).fetchone()[0] > 0


def suggestion_exists(conn: sqlite3.Connection, a_id: int, b_id: int) -> bool:
    """True if any grouping_suggestion (any status) exists for this tender pair."""
    return conn.execute("""
        SELECT 1 FROM grouping_suggestions
        WHERE member_a_type = 'tender' AND member_b_type = 'tender'
          AND ((member_a_id = ? AND member_b_id = ?)
           OR  (member_a_id = ? AND member_b_id = ?))
        LIMIT 1
    """, (a_id, b_id, b_id, a_id)).fetchone() is not None


# ---------------------------------------------------------------------------
# signal evaluation
# ---------------------------------------------------------------------------

def evaluate_signals(a: dict, b: dict) -> dict[str, bool]:
    """Evaluate the four grouping signals for a candidate pair."""

    # 1. Scheme: exact match; both must be a real (non-unknown) scheme.
    scheme = (
        bool(a["scheme"])
        and a["scheme"] != "unknown"
        and a["scheme"] == b["scheme"]
    )

    # 2. City: non-empty intersection of cities arrays.
    cities_a = parse_cities(a.get("cities"))
    cities_b = parse_cities(b.get("cities"))
    city = bool(cities_a and cities_b and cities_a & cities_b)

    # 3. Bus count: within 2%.
    ba, bb = a.get("bus_count"), b.get("bus_count")
    count = bool(
        ba and bb
        and max(ba, bb) > 0
        and abs(ba - bb) / max(ba, bb) <= 0.02
    )

    # 4. Dates: bid_due_date within ±7 days; fall back to issue_date when absent.
    date_a = parse_date(a.get("bid_due_date")) or parse_date(a.get("issue_date"))
    date_b = parse_date(b.get("bid_due_date")) or parse_date(b.get("issue_date"))
    dates = bool(date_a and date_b and abs((date_a - date_b).days) <= DATE_WINDOW_DAYS)

    return {"scheme": scheme, "city": city, "count": count, "dates": dates}


# ---------------------------------------------------------------------------
# group management
# ---------------------------------------------------------------------------

def _canonical_label(a: dict, b: dict, shared_city: str | None) -> str:
    scheme_str = SCHEME_LABEL.get(a["scheme"], a["scheme"])
    rep = a if source_priority(a["source_key"]) <= source_priority(b["source_key"]) else b
    bus_count = rep.get("bus_count") or a.get("bus_count") or b.get("bus_count")
    parts = [scheme_str]
    if shared_city:
        parts.append(shared_city.title())
    if bus_count:
        parts.append(f"{bus_count:,}-bus")
    model = rep.get("procurement_model")
    if model and model not in (None, "unknown"):
        parts.append(model.upper())
    return " ".join(parts)


def find_or_create_group(
    conn: sqlite3.Connection,
    a: dict,
    b: dict,
    signals: dict[str, bool],
    verified_by: str,
) -> int:
    """Return the group_id that should cover this pair, creating one if needed.

    Always re-reads group_id from the DB so writes made earlier in the same
    detection pass are visible. Returns -1 on an irreconcilable conflict
    (both members already belong to different groups).
    """
    # Re-read from DB in case an earlier pair in this pass already set group_id.
    row_a = conn.execute("SELECT group_id FROM tenders WHERE id = ?", (a["id"],)).fetchone()
    row_b = conn.execute("SELECT group_id FROM tenders WHERE id = ?", (b["id"],)).fetchone()
    gid_a = row_a["group_id"] if row_a else None
    gid_b = row_b["group_id"] if row_b else None

    if gid_a and gid_b:
        if gid_a == gid_b:
            return gid_a
        log.warning(
            "conflict: tender %d (group %d) and tender %d (group %d) matched "
            "but already belong to different groups — skipping to avoid overwrite",
            a["id"], gid_a, b["id"], gid_b,
        )
        return -1

    if gid_a:
        return gid_a
    if gid_b:
        return gid_b

    # Neither is grouped — create a new cluster.
    rep = a if source_priority(a["source_key"]) <= source_priority(b["source_key"]) else b
    cities_a = parse_cities(a.get("cities"))
    cities_b = parse_cities(b.get("cities"))
    shared = sorted(cities_a & cities_b)
    primary_city = shared[0] if shared else None
    label = _canonical_label(a, b, primary_city)
    conf  = lower_confidence(a.get("confidence"), b.get("confidence"))

    cur = conn.execute("""
        INSERT INTO tender_groups
            (canonical_label, scheme, primary_city,
             representative_member_type, representative_member_id,
             representative_bus_count, confidence, verified_by)
        VALUES (?, ?, ?, 'tender', ?, ?, ?, ?)
    """, (
        label, a["scheme"],
        primary_city.title() if primary_city else None,
        rep["id"], rep.get("bus_count"), conf, verified_by,
    ))
    return cur.lastrowid


# ---------------------------------------------------------------------------
# detection pass
# ---------------------------------------------------------------------------

def run_detection(conn: sqlite3.Connection) -> dict:
    """Evaluate all cross-source tender pairs; write groups or suggestions."""
    stats: dict[str, int] = {
        "pairs_evaluated":         0,
        "auto_grouped":            0,
        "queued":                  0,
        "deferred_multi_city":     0,
        "skipped_same_source":     0,
        "skipped_already_suggested": 0,
        "no_action":               0,
    }

    tenders = [dict(r) for r in conn.execute("""
        SELECT id, scheme, bus_count, cities, is_multi_city,
               bid_due_date, issue_date, source_key, confidence,
               procurement_model, group_id
        FROM tenders
        ORDER BY id
    """).fetchall()]

    log.info("detection: loaded %d tenders", len(tenders))
    n = len(tenders)

    for i in range(n):
        for j in range(i + 1, n):
            a, b = tenders[i], tenders[j]
            stats["pairs_evaluated"] += 1

            # Only cross-source observations are candidate pairs.
            if a["source_key"] == b["source_key"]:
                stats["skipped_same_source"] += 1
                log.debug("pair (%d,%d): same source '%s' — excluded", a["id"], b["id"], a["source_key"])
                continue

            # Any existing suggestion (any status) suppresses re-proposal.
            # Rejected pairs stay rejected; accepted pairs are already applied.
            if suggestion_exists(conn, a["id"], b["id"]):
                stats["skipped_already_suggested"] += 1
                log.debug("pair (%d,%d): suggestion already exists — skipped", a["id"], b["id"])
                continue

            # Strict multi-city gate: tender-level grouping is only valid for
            # single-city tenders. Multi-city tenders require lot-level signals,
            # which need lot data from PDF extraction.
            if a["is_multi_city"] or b["is_multi_city"]:
                stats["deferred_multi_city"] += 1
                if has_lots(conn, a["id"]) or has_lots(conn, b["id"]):
                    log.info(
                        "pair (%d,%d): multi-city with lots — lot-level grouping not yet implemented",
                        a["id"], b["id"],
                    )
                else:
                    log.info(
                        "pair (%d,%d): multi-city, no lots — deferred pending lot-level analysis",
                        a["id"], b["id"],
                    )
                continue

            signals = evaluate_signals(a, b)
            matched = sum(signals.values())
            score   = round(matched / 4.0, 2)
            log.info("pair (%d,%d) signals=%s score=%.2f", a["id"], b["id"], signals, score)

            if matched == 4:
                group_id = find_or_create_group(conn, a, b, signals, verified_by="cross_source")
                if group_id == -1:
                    continue
                for t in (a, b):
                    conn.execute(
                        "UPDATE tenders SET group_id = ? WHERE id = ? AND group_id IS NULL",
                        (group_id, t["id"]),
                    )
                stats["auto_grouped"] += 1
                log.info("pair (%d,%d): AUTO-GROUPED → group_id=%d", a["id"], b["id"], group_id)

            elif matched >= MIN_SIGNALS_FOR_SUGGESTION:
                conn.execute("""
                    INSERT INTO grouping_suggestions
                        (member_a_type, member_a_id, member_b_type, member_b_id,
                         signals_matched, match_score, status)
                    VALUES ('tender', ?, 'tender', ?, ?, ?, 'pending')
                """, (a["id"], b["id"], json.dumps(signals), score))
                stats["queued"] += 1
                log.info(
                    "pair (%d,%d): QUEUED → pending review (score=%.2f signals=%s)",
                    a["id"], b["id"], score, signals,
                )

            else:
                stats["no_action"] += 1
                log.debug("pair (%d,%d): %d signals — below threshold, no action", a["id"], b["id"], matched)

    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# apply pass
# ---------------------------------------------------------------------------

def run_apply(conn: sqlite3.Connection, reviewer: str) -> dict:
    """Apply accepted grouping_suggestions: create/attach group, set group_id."""
    stats: dict[str, int] = {"applied": 0, "skipped_non_tender": 0}

    accepted = [dict(r) for r in conn.execute("""
        SELECT id AS sg_id, member_a_type, member_a_id, member_b_type, member_b_id,
               signals_matched, suggested_group_id, reviewer AS existing_reviewer
        FROM grouping_suggestions
        WHERE status = 'accepted'
    """).fetchall()]

    log.info("apply: found %d accepted suggestion(s)", len(accepted))

    for sg in accepted:
        if sg["member_a_type"] != "tender" or sg["member_b_type"] != "tender":
            stats["skipped_non_tender"] += 1
            log.warning(
                "suggestion %d: non-tender member type — lot-level apply not yet implemented",
                sg["sg_id"],
            )
            continue

        ta = dict(conn.execute("SELECT * FROM tenders WHERE id = ?", (sg["member_a_id"],)).fetchone())
        tb = dict(conn.execute("SELECT * FROM tenders WHERE id = ?", (sg["member_b_id"],)).fetchone())
        signals = json.loads(sg["signals_matched"] or "{}")

        group_id = sg["suggested_group_id"]
        if group_id is None:
            group_id = find_or_create_group(conn, ta, tb, signals, verified_by="human")
            if group_id == -1:
                continue
            conn.execute(
                "UPDATE grouping_suggestions SET suggested_group_id = ? WHERE id = ?",
                (group_id, sg["sg_id"]),
            )

        for t in (ta, tb):
            conn.execute(
                "UPDATE tenders SET group_id = ? WHERE id = ? AND group_id IS NULL",
                (group_id, t["id"]),
            )

        # Stamp reviewer only if not already set (preserves any value written by a review UI).
        conn.execute("""
            UPDATE grouping_suggestions
            SET reviewer    = COALESCE(reviewer, ?),
                reviewed_at = COALESCE(reviewed_at, datetime('now'))
            WHERE id = ?
        """, (reviewer, sg["sg_id"]))

        stats["applied"] += 1
        log.info("suggestion %d applied → group_id=%d (reviewer: %s)", sg["sg_id"], group_id, reviewer)

    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# fixture mode
# ---------------------------------------------------------------------------

def _fixture_db() -> sqlite3.Connection:
    """In-memory DB with full schema applied. Never touches data/evbus.db."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript((REPO_ROOT / "db" / "schema.sql").read_text())
    return conn


def _ins(conn: sqlite3.Connection, title: str, scheme: str, source_key: str,
         cities: list, bus_count: int, bid_due_date: str, is_multi_city: int,
         dkey: str) -> int:
    cur = conn.execute("""
        INSERT INTO tenders
            (title, scheme, source_key, cities, bus_count, bid_due_date,
             is_multi_city, status, confidence, dedupe_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 'reported', ?)
    """, (title, scheme, source_key, json.dumps(cities), bus_count, bid_due_date,
          is_multi_city, dkey))
    return cur.lastrowid


def run_fixture() -> None:
    """Detection dry-run on in-memory DB; prints PASS/FAIL for all four cases."""
    print("=== FIXTURE MODE — in-memory DB, not touching data/evbus.db ===\n")

    conn = _fixture_db()

    # (a) All 4 signals match, both single-city — should auto-group.
    a_id = _ins(conn, "Chennai e-Bus Sewa (CESL source)",   "pm_ebus_sewa", "cesl", ["Chennai"],   500,  "2026-08-01", 0, "fix-a1")
    b_id = _ins(conn, "Chennai e-Bus Sewa (CPPP source)",   "pm_ebus_sewa", "cppp", ["Chennai"],   500,  "2026-08-01", 0, "fix-a2")

    # (b) Scheme differs; city/count/dates match → 3-of-4 → review queue.
    c_id = _ins(conn, "Bangalore E-DRIVE (CESL source)",    "pm_edrive",    "cesl", ["Bangalore"], 300,  "2026-09-01", 0, "fix-b1")
    d_id = _ins(conn, "Bangalore e-Bus Sewa (CPPP source)", "pm_ebus_sewa", "cppp", ["Bangalore"], 300,  "2026-09-01", 0, "fix-b2")

    # (c) Both multi-city, no lots — strict gate, should be deferred with no writes.
    e_id = _ins(conn, "Multi-city e-Bus Sewa (CESL source)", "pm_ebus_sewa", "cesl", [],          2000, "2026-10-01", 1, "fix-c1")
    f_id = _ins(conn, "Multi-city e-Bus Sewa (CPPP source)", "pm_ebus_sewa", "cppp", [],          2000, "2026-10-01", 1, "fix-c2")

    # (d) Same source_key (cesl+cesl), all signals identical — excluded before evaluation.
    g_id = _ins(conn, "Mumbai e-Bus Sewa obs-1 (CESL)",     "pm_ebus_sewa", "cesl", ["Mumbai"],   400,  "2026-11-01", 0, "fix-d1")
    h_id = _ins(conn, "Mumbai e-Bus Sewa obs-2 (CESL)",     "pm_ebus_sewa", "cesl", ["Mumbai"],   400,  "2026-11-01", 0, "fix-d2")

    conn.commit()

    print("Seeded 8 synthetic tenders:")
    print(f"  (a) auto-group:      id={a_id} cesl/pm_ebus_sewa/Chennai/500   +  id={b_id} cppp/pm_ebus_sewa/Chennai/500")
    print(f"  (b) review queue:    id={c_id} cesl/pm_edrive/Bangalore/300    +  id={d_id} cppp/pm_ebus_sewa/Bangalore/300")
    print(f"  (c) multi-city gate: id={e_id} cesl/pm_ebus_sewa/[]/2000/mc=1  +  id={f_id} cppp/pm_ebus_sewa/[]/2000/mc=1")
    print(f"  (d) same-source:     id={g_id} cesl/pm_ebus_sewa/Mumbai/400    +  id={h_id} cesl/pm_ebus_sewa/Mumbai/400")
    print()

    stats = run_detection(conn)

    print("\n--- Detection stats ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()

    # --- (a) auto-group ---
    r_a1 = conn.execute("SELECT group_id FROM tenders WHERE id = ?", (a_id,)).fetchone()
    r_a2 = conn.execute("SELECT group_id FROM tenders WHERE id = ?", (b_id,)).fetchone()
    gid_a, gid_b = (r_a1["group_id"] if r_a1 else None), (r_a2["group_id"] if r_a2 else None)
    if gid_a and gid_a == gid_b:
        grp = conn.execute(
            "SELECT canonical_label, verified_by, confidence FROM tender_groups WHERE id = ?", (gid_a,)
        ).fetchone()
        print(
            f"(a) AUTO-GROUP:      PASS\n"
            f"     tenders {a_id}+{b_id} both have group_id={gid_a}\n"
            f"     label='{grp['canonical_label']}'  verified_by={grp['verified_by']}  confidence={grp['confidence']}"
        )
    else:
        print(f"(a) AUTO-GROUP:      FAIL — group_ids=({gid_a},{gid_b})")

    # --- (b) review queue ---
    sg_b = conn.execute("""
        SELECT * FROM grouping_suggestions WHERE member_a_type='tender' AND member_b_type='tender'
          AND ((member_a_id=? AND member_b_id=?) OR (member_a_id=? AND member_b_id=?))
    """, (c_id, d_id, d_id, c_id)).fetchone()
    if sg_b and sg_b["status"] == "pending":
        sigs = json.loads(sg_b["signals_matched"])
        n_matched = sum(1 for v in sigs.values() if v)
        print(
            f"(b) REVIEW QUEUE:    PASS\n"
            f"     suggestion id={sg_b['id']}  status=pending  score={sg_b['match_score']}\n"
            f"     signals={sigs}  ({n_matched}/4 matched — scheme fails because pm_edrive≠pm_ebus_sewa)"
        )
    else:
        sg_b_status = "not found" if not sg_b else f"status={sg_b['status']}"
        print(f"(b) REVIEW QUEUE:    FAIL — {sg_b_status}")

    # --- (c) multi-city gate ---
    r_e = conn.execute("SELECT group_id FROM tenders WHERE id = ?", (e_id,)).fetchone()
    r_f = conn.execute("SELECT group_id FROM tenders WHERE id = ?", (f_id,)).fetchone()
    sg_c = conn.execute("""
        SELECT 1 FROM grouping_suggestions WHERE member_a_type='tender' AND member_b_type='tender'
          AND ((member_a_id=? AND member_b_id=?) OR (member_a_id=? AND member_b_id=?))
    """, (e_id, f_id, f_id, e_id)).fetchone()
    gid_e = r_e["group_id"] if r_e else "N/A"
    gid_f = r_f["group_id"] if r_f else "N/A"
    if gid_e is None and gid_f is None and sg_c is None:
        print(
            f"(c) MULTI-CITY GATE: PASS\n"
            f"     tenders {e_id}+{f_id} untouched — no group_id set, no suggestion filed\n"
            f"     (deferred_multi_city={stats['deferred_multi_city']}; pair blocked before signal evaluation)"
        )
    else:
        print(f"(c) MULTI-CITY GATE: FAIL — group_ids=({gid_e},{gid_f})  suggestion={'found' if sg_c else 'none'}")

    # --- (d) same-source exclusion ---
    r_g = conn.execute("SELECT group_id FROM tenders WHERE id = ?", (g_id,)).fetchone()
    r_h = conn.execute("SELECT group_id FROM tenders WHERE id = ?", (h_id,)).fetchone()
    sg_d = conn.execute("""
        SELECT 1 FROM grouping_suggestions WHERE member_a_type='tender' AND member_b_type='tender'
          AND ((member_a_id=? AND member_b_id=?) OR (member_a_id=? AND member_b_id=?))
    """, (g_id, h_id, h_id, g_id)).fetchone()
    gid_g = r_g["group_id"] if r_g else "N/A"
    gid_h = r_h["group_id"] if r_h else "N/A"
    if gid_g is None and gid_h is None and sg_d is None:
        print(
            f"(d) SAME-SOURCE:     PASS\n"
            f"     tenders {g_id}+{h_id} untouched — excluded before signal evaluation\n"
            f"     (skipped_same_source={stats['skipped_same_source']}; both source_key='cesl')"
        )
    else:
        print(f"(d) SAME-SOURCE:     FAIL — group_ids=({gid_g},{gid_h})  suggestion={'found' if sg_d else 'none'}")

    print("\n=== Fixture complete. ===")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(description="Detect and apply cross-source tender groupings.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true",
        help="Apply accepted grouping_suggestions: set group_id, stamp reviewer.",
    )
    mode.add_argument(
        "--fixture", action="store_true",
        help="Run against in-memory fixture DB (does not touch data/evbus.db).",
    )
    parser.add_argument(
        "--reviewer", default="human",
        help="Reviewer name stamped on --apply runs (default: 'human').",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="DEBUG-level pair output.")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.fixture:
        run_fixture()
        return

    conn = get_db()

    if args.apply:
        stats = run_apply(conn, args.reviewer)
        print(f"apply: applied={stats['applied']}")
    else:
        stats = run_detection(conn)
        print(
            f"detection: "
            f"pairs_evaluated={stats['pairs_evaluated']}  "
            f"auto_grouped={stats['auto_grouped']}  "
            f"queued={stats['queued']}  "
            f"deferred_multi_city={stats['deferred_multi_city']}  "
            f"skipped_same_source={stats['skipped_same_source']}  "
            f"no_action={stats['no_action']}"
        )


if __name__ == "__main__":
    main()
