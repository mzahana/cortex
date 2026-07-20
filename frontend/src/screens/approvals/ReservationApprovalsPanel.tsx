import { Alert, Button, Center, Loader, Pagination, Stack, Text } from "@mantine/core";
import type { Me } from "../../api/types";
import { useReservationList } from "../reservations/useReservationList";
import { ReservationListItem } from "../reservations/ReservationListItem";

interface ReservationApprovalsPanelProps {
  me: Me;
}

/**
 * Pending-reservation approvals (T3.4 Approvals screen, `docs/api-and-ui.md`:
 * "Pending reservation/reorder approvals in my scope"). `GET /reservations?
 * status=pending` — `apps.reservations.api.ReservationViewSet.
 * filterset_fields` documents `status` as a supported filter, so this is a
 * real server-side filter, not a client-side scan. The list is already
 * scope-filtered server-side to what the caller can `asset.view`; the
 * approve/reject buttons on each row are further gated by
 * `reservation.approve` in that row's actual project scope (see
 * `ReservationListItem`) — a pending item the caller can see but not approve
 * (e.g. a plain Member) simply renders with no action buttons.
 */
export function ReservationApprovalsPanel({ me }: ReservationApprovalsPanelProps) {
  const filters = { status: "pending" as const, ordering: "start_at" as const };
  const { items, assetsById, totalCount, page, pageCount, loading, error, setPage, reload } =
    useReservationList({ filters });

  const handleChanged = () => {
    reload();
  };

  return (
    <Stack gap="sm">
      <Text size="sm" c="dimmed">
        {totalCount !== null ? `${items.length} of ${totalCount} pending` : ""}
      </Text>

      {error && (
        <Alert color="red" title="Couldn't load reservation approvals">
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
          <Loader />
        </Center>
      )}

      {!loading && !error && items.length === 0 && (
        <Center p="xl">
          <Text c="dimmed">No pending reservations.</Text>
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
