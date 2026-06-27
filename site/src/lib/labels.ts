// Shared display-label mappings for controlled-vocabulary fields.
//
// The site stores the raw controlled value (e.g. scheme = 'pm_edrive') and maps
// to a human label only at render time. Filters/data-attributes must match on
// the RAW value; only the visible text uses the label. This replaces the
// label-overwrite that used to live in the merge step, so the JSON keeps the
// machine value and the page owns presentation.

// scheme enum -> human display label. Unmapped values pass through unchanged.
export const SCHEME_LABEL: Record<string, string> = {
  pm_ebus_sewa: 'PM e-Bus Sewa',
  pm_edrive: 'PM E-DRIVE',
  fame_2: 'FAME II',
  state_funded: 'State-funded',
  smart_city: 'Smart City',
  other: 'Other',
  unknown: 'Unknown',
};

export function schemeLabel(s: string | null | undefined): string {
  return s ? (SCHEME_LABEL[s] ?? s) : '—';
}
