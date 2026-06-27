// Shared tender status display logic — the single source of the badge computation
// that was previously duplicated inline across five pages.
//
// The DB stores a RAW status (derived by trg_tenders_status_from_events) and the
// site computes the *displayed* badge from that status plus the bid deadline.
// 'under_evaluation' is a DISPLAY-ONLY state: the DB keeps the valid raw status
// 'bids_opened' (no schema/CHECK change), and this helper maps it to a friendlier
// in-progress badge that decays to needs_review after 180 days.

const DAY_MS = 864e5;
const EVAL_DECAY_DAYS = 180;

interface StatusHistoryEntry {
  event_type?: string;
  event_date?: string | null;
}

interface TenderLike {
  status?: string | null;
  bid_due_date?: string | null;
  status_history?: StatusHistoryEntry[] | null;
}

/**
 * Compute the displayed status badge for a tender, as of `buildDate`.
 *
 * Precedence (highest first):
 *   1. awarded   -> 'awarded'
 *   2. cancelled -> 'cancelled'
 *   3. extended  -> 'extended'
 *   4. bids_opened -> 'under_evaluation', unless >180 days since the bids_opened
 *      event_date, in which case it has decayed to 'needs_review'.
 *   5. deadline logic: past -> needs_review; <=7 days -> closing_soon; else open.
 *
 * Existing statuses (open/closing_soon/needs_review/awarded/cancelled/extended)
 * are unchanged; only 'bids_opened' gains new display behaviour.
 */
export function effectiveStatus(t: TenderLike, buildDate: Date): string {
  const declared = (t.status as string) ?? 'unknown';
  if (declared === 'awarded') return 'awarded';
  if (declared === 'cancelled') return 'cancelled';
  if (declared === 'extended') return 'extended';

  if (declared === 'bids_opened') {
    // Decay is keyed off the bids_opened EVENT_DATE pulled from status_history.
    const opened = (t.status_history ?? []).find((e) => e.event_type === 'bids_opened');
    // KNOWN UNHANDLED EDGE: a bids_opened event dated after a still-future
    // bid_due_date is an impossible sequence and is NOT validated here — no such
    // data exists yet; revisit when a validation pass / diff engine lands.
    if (opened?.event_date) {
      const daysSince = Math.floor(
        (buildDate.getTime() - new Date(opened.event_date).getTime()) / DAY_MS,
      );
      return daysSince > EVAL_DECAY_DAYS ? 'needs_review' : 'under_evaluation';
    }
    // Guard: status says bids_opened but status_history has no bids_opened entry
    // (should not happen — the trigger sets this status from that very event).
    // Fall through to the deadline logic below rather than crash on a missing date.
  }

  const dl: string | null = t.bid_due_date ?? null;
  if (!dl) return declared || 'unknown';
  const diffDays = Math.ceil((new Date(dl).getTime() - buildDate.getTime()) / DAY_MS);
  // 'extended' is already handled at precedence 3, so a past deadline here is
  // unambiguously needs_review.
  if (diffDays < 0) return 'needs_review';
  if (diffDays <= 7) return 'closing_soon';
  return 'open';
}
