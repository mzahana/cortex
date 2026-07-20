import { useState } from "react";
import { Alert, Badge, Button, Group, Stack, Text } from "@mantine/core";
import { api, ApiError } from "../../api/client";
import { hasAssetPermission, RESERVATION_APPROVE } from "../../api/permissions";
import type { Asset, Me, Reservation, ReservationStatus } from "../../api/types";

const STATUS_COLORS: Record<ReservationStatus, string> = {
  pending: "yellow",
  approved: "blue",
  rejected: "red",
  cancelled: "gray",
  fulfilled: "green",
  expired: "gray",
};

function formatWindow(startAt: string, endAt: string): string {
  const start = new Date(startAt);
  const end = new Date(endAt);
  const sameDay =
    start.getFullYear() === end.getFullYear() &&
    start.getMonth() === end.getMonth() &&
    start.getDate() === end.getDate();
  const dateFmt: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
  const timeFmt: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };
  if (sameDay) {
    return `${start.toLocaleDateString(undefined, dateFmt)}, ${start.toLocaleTimeString(
      undefined,
      timeFmt,
    )} – ${end.toLocaleTimeString(undefined, timeFmt)}`;
  }
  return `${start.toLocaleDateString(undefined, dateFmt)} ${start.toLocaleTimeString(
    undefined,
    timeFmt,
  )} – ${end.toLocaleDateString(undefined, dateFmt)} ${end.toLocaleTimeString(undefined, timeFmt)}`;
}

interface ReservationListItemProps {
  reservation: Reservation;
  asset: Asset | undefined;
  me: Me;
  onChanged: (updated: Reservation) => void;
}

/**
 * One reservation row — used by both the Calendar's day agenda and the
 * Approvals screen. Approve/reject in-place for a scoped approver
 * (`reservation.approve`); cancel for the requester or a scoped approver.
 * Every action is presentation-gated only (CLAUDE.md: a 403 from the server
 * is a normal, handled outcome).
 */
export function ReservationListItem({ reservation, asset, me, onChanged }: ReservationListItemProps) {
  const [busy, setBusy] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);

  const projectId = asset?.project ?? null;
  const canApprove = hasAssetPermission(me, RESERVATION_APPROVE, projectId);
  const isOwn = reservation.user === me.id;
  const canCancel = (isOwn || canApprove) && ["pending", "approved"].includes(reservation.status);

  const run = async (action: () => Promise<Reservation>) => {
    setBusy(true);
    setRowError(null);
    try {
      const updated = await action();
      onChanged(updated);
    } catch (err) {
      setRowError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack gap={4} p="sm" style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 8 }}>
      <Group justify="space-between" wrap="nowrap">
        <Text fw={600} size="sm" truncate>
          {asset?.name ?? `Asset #${reservation.asset}`}
        </Text>
        <Badge color={STATUS_COLORS[reservation.status]} size="sm">
          {reservation.status}
        </Badge>
      </Group>
      <Text size="xs" c="dimmed">
        {formatWindow(reservation.start_at, reservation.end_at)}
      </Text>
      {isOwn && (
        <Text size="xs" c="dimmed">
          Your booking
        </Text>
      )}
      {reservation.approval_note && (
        <Text size="xs" c="dimmed">
          Note: {reservation.approval_note}
        </Text>
      )}

      {rowError && (
        <Alert color="red" py={4} px="xs">
          <Text size="xs">{rowError}</Text>
        </Alert>
      )}

      {(reservation.status === "pending" && canApprove) || canCancel ? (
        <Group gap="xs" mt={4}>
          {reservation.status === "pending" && canApprove && (
            <>
              <Button
                size="xs"
                loading={busy}
                onClick={() => void run(() => api.approveReservation(reservation.id))}
                data-testid={`approve-${reservation.id}`}
              >
                Approve
              </Button>
              <Button
                size="xs"
                color="red"
                variant="light"
                loading={busy}
                onClick={() => void run(() => api.rejectReservation(reservation.id))}
                data-testid={`reject-${reservation.id}`}
              >
                Reject
              </Button>
            </>
          )}
          {canCancel && (
            <Button
              size="xs"
              variant="subtle"
              color="gray"
              loading={busy}
              onClick={() => void run(() => api.cancelReservation(reservation.id))}
            >
              Cancel
            </Button>
          )}
        </Group>
      ) : null}
    </Stack>
  );
}
