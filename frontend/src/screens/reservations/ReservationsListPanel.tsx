import { useMemo, useState } from "react";
import { Alert, Button, Center, Group, Loader, Pagination, Select, Stack, Text } from "@mantine/core";
import type { Asset, Me, ReservationListParams, ReservationStatus } from "../../api/types";
import { AssetPickerSelect } from "./AssetPickerSelect";
import { ReservationListItem, STATUS_COLORS } from "./ReservationListItem";
import { useReservationList } from "./useReservationList";

interface ReservationsListPanelProps {
  me: Me;
}

/** `ReservationStatus` -> label, in the order the filter dropdown offers
 * them — "All" first, then every status `STATUS_COLORS` (`ReservationListItem`)
 * already knows how to badge, so a newly-added status only needs to be added
 * there to automatically show up correctly here too. */
const STATUS_FILTER_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All statuses" },
  ...(Object.keys(STATUS_COLORS) as ReservationStatus[]).map((status) => ({
    value: status,
    label: status.charAt(0).toUpperCase() + status.slice(1),
  })),
];

const ORDERING_OPTIONS: { value: NonNullable<ReservationListParams["ordering"]>; label: string }[] = [
  { value: "-start_at", label: "Start date (newest first)" },
  { value: "start_at", label: "Start date (oldest first)" },
  { value: "-created_at", label: "Requested (newest first)" },
  { value: "created_at", label: "Requested (oldest first)" },
];

/**
 * Reservations List (user-requested, post-MVP — filterable table/list
 * alternative to the Calendar's month/week/day agenda for finding a specific
 * reservation to act on at scale). Server-side filtered/paginated via the
 * same `useReservationList` hook and `GET /api/v1/reservations` the Calendar
 * and Approvals screens already use — `status`/`asset` map straight onto
 * `ReservationViewSet.filterset_fields`, `ordering` onto its
 * `ordering_fields`. Rows are the exact same `ReservationListItem` (status
 * badge + approve/reject/cancel/checkout/checkin, permission-gated) used
 * everywhere else, so this adds a browsing/filtering surface only — no new
 * action logic, no new backend endpoint, no client-side scan of "all
 * reservations".
 */
export function ReservationsListPanel({ me }: ReservationsListPanelProps) {
  const [status, setStatus] = useState<string | null>(null);
  const [asset, setAsset] = useState<Asset | null>(null);
  const [ordering, setOrdering] = useState<NonNullable<ReservationListParams["ordering"]>>("-start_at");

  const filters = useMemo<ReservationListParams>(
    () => ({
      status: status ? (status as ReservationStatus) : undefined,
      asset: asset ? asset.id : undefined,
      ordering,
    }),
    [status, asset, ordering],
  );

  const { items, assetsById, totalCount, page, pageCount, loading, error, setPage, reload } = useReservationList({
    filters,
  });

  const handleChanged = () => {
    reload();
  };

  return (
    <Stack gap="sm">
      <Stack gap="xs">
        <Group gap="xs" wrap="wrap">
          <Select
            placeholder="Status"
            data={STATUS_FILTER_OPTIONS}
            value={status ?? ""}
            onChange={(v) => setStatus(v || null)}
            w={170}
            aria-label="Filter by status"
            data-testid="reservation-list-status-filter"
          />
          <AssetPickerSelect
            value={asset}
            onChange={setAsset}
            placeholder="Asset"
            w={220}
            aria-label="Filter by asset"
          />
          <Select
            placeholder="Sort"
            data={ORDERING_OPTIONS}
            value={ordering}
            onChange={(v) => v && setOrdering(v as NonNullable<ReservationListParams["ordering"]>)}
            w={220}
            aria-label="Sort order"
            allowDeselect={false}
          />
        </Group>
        <Text size="sm" c="dimmed">
          {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
        </Text>
      </Stack>

      {error && (
        <Alert color="red" title="Couldn't load reservations">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{error}</Text>
            <Button size="xs" variant="light" onClick={reload}>
              Retry
            </Button>
          </Stack>
        </Alert>
      )}

      {loading && !error && (
        <Center p="xl">
          <Loader data-testid="reservations-list-loading" />
        </Center>
      )}

      {!loading && !error && items.length === 0 && (
        <Center p="xl">
          <Text c="dimmed">No reservations match these filters.</Text>
        </Center>
      )}

      {!loading && !error && items.length > 0 && (
        <Stack gap="sm">
          {items.map((r) => (
            <ReservationListItem
              key={r.id}
              reservation={r}
              asset={assetsById.get(r.asset)}
              me={me}
              onChanged={handleChanged}
            />
          ))}
        </Stack>
      )}

      {pageCount > 1 && (
        <Center>
          <Pagination total={pageCount} value={page} onChange={setPage} size="sm" />
        </Center>
      )}
    </Stack>
  );
}
