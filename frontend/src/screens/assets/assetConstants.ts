import type { AssetOrdering, AssetStatus } from "../../api/types";

/** `Asset.Status` choices (`backend/apps/assets/models.py`) — kept as a
 * typed lookup here rather than fetched from the server since it's a fixed
 * enum, same convention as `CustomFieldDataType` in `api/types.ts`. */
export const STATUS_OPTIONS: { value: AssetStatus; label: string }[] = [
  { value: "available", label: "Available" },
  { value: "in_use", label: "In use" },
  { value: "reserved", label: "Reserved" },
  { value: "maintenance", label: "Maintenance" },
  { value: "retired", label: "Retired" },
  { value: "lost", label: "Lost" },
];

export const STATUS_COLORS: Record<AssetStatus, string> = {
  available: "green",
  in_use: "blue",
  reserved: "grape",
  maintenance: "orange",
  retired: "gray",
  lost: "red",
};

export const STATUS_LABELS: Record<AssetStatus, string> = Object.fromEntries(
  STATUS_OPTIONS.map((o) => [o.value, o.label]),
) as Record<AssetStatus, string>;

/**
 * `"relevance"` is a client-only sentinel (never sent as `?ordering=` at
 * all) — T1.6 review carried fix: `apps.assets.api.AssetSearchFilter` only
 * ranks by `-rank` (FTS/trigram relevance) when the request has NO
 * `ordering` param; the client previously always sent one (defaulting to
 * `-created_at`), so the server's relevance branch never actually ran
 * during a search. `AssetOrderingOrRelevance` values map 1:1 onto a real
 * `?ordering=` value EXCEPT this one, which `AssetListScreen` omits from the
 * query entirely, letting the server's own default (relevance-when-
 * searching, `-created_at` otherwise) take over. It's the list's default
 * selection precisely because "the user hasn't explicitly chosen a sort" and
 * "ordering is omitted" are the same state.
 */
export type AssetOrderingOrRelevance = AssetOrdering | "relevance";

/** `?ordering=` whitelist (`apps.assets.api.AssetViewSet.ordering_fields`),
 * plus the client-only `"relevance"` sentinel above. */
export const ORDERING_OPTIONS: { value: AssetOrderingOrRelevance; label: string }[] = [
  { value: "relevance", label: "Relevance (best match)" },
  { value: "-created_at", label: "Newest first" },
  { value: "created_at", label: "Oldest first" },
  { value: "name", label: "Name (A→Z)" },
  { value: "-name", label: "Name (Z→A)" },
  { value: "status", label: "Status" },
  { value: "-status", label: "Status (reverse)" },
  { value: "-purchase_date", label: "Purchase date (newest)" },
  { value: "purchase_date", label: "Purchase date (oldest)" },
];
