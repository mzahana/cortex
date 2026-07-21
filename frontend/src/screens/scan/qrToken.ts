/**
 * Pulls a `qr_token` out of whatever text a decoded QR code (or a manually
 * typed/pasted value) contains. T4.5's label generator encodes each asset's
 * stable `qr_token`/URL (`docs/tasks/M4-mobile-scan-labels.md`) — support
 * both shapes so a scan round-trips correctly regardless of which the label
 * PDF ends up using:
 *   - a bare token (e.g. "Ab12_xyz-…") -> returned as-is
 *   - a full URL whose last path segment (or `?token=`/`?qr_token=` query
 *     param) is the token (e.g. "https://cortex.example.com/api/v1/resolve/
 *     Ab12_xyz-…" or ".../scan?token=Ab12_xyz-…") -> the token is extracted
 */
export function extractQrToken(raw: string): string {
  const text = raw.trim();
  if (!text) return "";

  try {
    const url = new URL(text);
    const queryToken = url.searchParams.get("token") ?? url.searchParams.get("qr_token");
    if (queryToken) return queryToken;

    const segments = url.pathname.split("/").filter(Boolean);
    if (segments.length > 0) return segments[segments.length - 1];

    return text;
  } catch {
    // Not a URL — treat the whole trimmed string as the token itself.
    return text;
  }
}

/** True if the manual-entry value looks like a plain numeric `Asset.id`
 * rather than a `qr_token` — lets the manual fallback (risk R5) resolve
 * either shape the task doc calls for ("manual token/asset-ID entry"). */
export function isNumericAssetId(value: string): boolean {
  return /^\d+$/.test(value.trim());
}
