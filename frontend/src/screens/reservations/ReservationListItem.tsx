import { useEffect, useState } from "react";
import { Alert, Badge, Button, Group, Stack, Text } from "@mantine/core";
import { api, ApiError } from "../../api/client";
import {
  CHECKOUT_MANAGE,
  hasAssetPermission,
  RESERVATION_APPROVE,
} from "../../api/permissions";
import type { Asset, Checkout, Me, Reservation, ReservationStatus } from "../../api/types";
import { CheckoutModal } from "../assets/CheckoutModal";

/** Shared `Reservation.status` -> Mantine color mapping — also reused by
 * `AssetReservationMonthCalendar`'s bar coloring so the month-grid bars match
 * this row's badge colors exactly (per the month-view spec's requirement to
 * reuse, not invent, status colors). */
export const STATUS_COLORS: Record<ReservationStatus, string> = {
  pending: "yellow",
  approved: "blue",
  rejected: "red",
  cancelled: "gray",
  fulfilled: "green",
  expired: "gray",
};

/** Exported for reuse by `AssetReservationMonthCalendar`'s bar-tap popover,
 * which wants the same "Mon, Jan 2, 10:00 – 12:00" formatting this row uses. */
export function formatWindow(startAt: string, endAt: string): string {
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
 * One reservation row — used by the Calendar's day agenda, the Approvals
 * screen, and the Asset Detail "Reservations" section. Approve/reject
 * in-place for a scoped approver (`reservation.approve`); cancel for the
 * requester or a scoped approver; check-out/check-in for an approved booking
 * (reservation-first checkout, `docs/api-and-ui.md` "Reservations & checkout").
 * Every action is presentation-gated only (CLAUDE.md: a 403 from the server
 * is a normal, handled outcome).
 */
export function ReservationListItem({ reservation, asset, me, onChanged }: ReservationListItemProps) {
  const [busy, setBusy] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);
  const [checkoutModalOpen, setCheckoutModalOpen] = useState(false);
  const [openCheckout, setOpenCheckout] = useState<Checkout | null>(null);
  const [checkinBusy, setCheckinBusy] = useState(false);

  const projectId = asset?.project ?? null;
  const canApprove = hasAssetPermission(me, RESERVATION_APPROVE, projectId);
  const isOwn = reservation.user.id === me.id;
  const canCancel = (isOwn || canApprove) && ["pending", "approved"].includes(reservation.status);
  // `checkout.manage` gates both check-out and self-service check-in (the
  // server additionally requires the caller be the checkout's HOLDER for
  // check-in specifically, same caveat `CHECKOUT_MANAGE`'s own doc comment
  // makes) — this component only ever offers check-in for the requester's
  // own booking, mirroring `AssetDetailScreen`'s "my open checkout" pattern.
  const canCheckout = hasAssetPermission(me, CHECKOUT_MANAGE, projectId);
  const isCheckoutRelevant = reservation.status === "approved" && (isOwn || canCheckout);

  // Resolve "is there already an open checkout for THIS reservation" via the
  // dedicated `?reservation=&open=true` filter (Contract 2) — no client-side
  // scan of every open checkout needed.
  useEffect(() => {
    let cancelled = false;
    if (!isCheckoutRelevant) {
      setOpenCheckout(null);
      return;
    }
    api
      .listCheckouts({ reservation: reservation.id, open: true, page_size: 1 })
      .then((body) => {
        if (!cancelled) setOpenCheckout(body.results[0] ?? null);
      })
      .catch(() => {
        if (!cancelled) setOpenCheckout(null);
      });
    return () => {
      cancelled = true;
    };
  }, [reservation.id, isCheckoutRelevant]);

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

  const handleCheckedOut = (checkout: Checkout) => {
    setOpenCheckout(checkout);
    onChanged(reservation);
  };

  const handleCheckIn = async () => {
    if (!openCheckout) return;
    setCheckinBusy(true);
    setRowError(null);
    try {
      await api.checkinCheckout(openCheckout.id);
      setOpenCheckout(null);
      onChanged(reservation);
    } catch (err) {
      // A server 403/409 here is a normal, handled outcome (CLAUDE.md) — the
      // client gate above can drift from the server's own scoped/holder check.
      setRowError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setCheckinBusy(false);
    }
  };

  // No client-side time-window gate here: the server's own checkout
  // validation (`CheckoutSerializer.validate`, `backend/apps/reservations/
  // checkout.py`) never checks `start_at`/`end_at` at all — it only requires
  // `approved`/`fulfilled` status, caller ownership, and a matching asset.
  // For a `requires_approval` category this reservation row is the ONLY path
  // to checkout (direct Asset Detail checkout is server-rejected without a
  // reservation), so gating the button on a window the server doesn't
  // enforce created a real dead-end (e.g. arriving early, or shortly after
  // `end_at` before the reservation expires/is cancelled). The server is the
  // sole authority on timing; this only decides eligibility + no duplicate
  // open checkout.
  const showCheckoutButton = isCheckoutRelevant && !openCheckout && !!asset && !asset.is_consumable;
  // Check-in is self-service only (mirrors `AssetDetailScreen`'s
  // `handleCheckIn`) — a scoped approver who isn't the holder must use the
  // Asset Detail / My Items override flow instead, not this row.
  const showCheckinButton = isOwn && !!openCheckout;

  const hasAnyAction =
    (reservation.status === "pending" && canApprove) || canCancel || showCheckoutButton || showCheckinButton;

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
      <Text size="xs" c="dimmed">
        {isOwn ? "Your booking" : `Requested by: ${reservation.user.name || reservation.user.email}`}
      </Text>
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

      {hasAnyAction ? (
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
          {showCheckoutButton && (
            <Button
              size="xs"
              variant="filled"
              onClick={() => setCheckoutModalOpen(true)}
              data-testid={`checkout-from-reservation-${reservation.id}`}
            >
              Check out
            </Button>
          )}
          {showCheckinButton && (
            <Button
              size="xs"
              variant="filled"
              color="teal"
              loading={checkinBusy}
              onClick={() => void handleCheckIn()}
              data-testid={`checkin-from-reservation-${reservation.id}`}
            >
              Check in
            </Button>
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

      {asset && (
        <CheckoutModal
          opened={checkoutModalOpen}
          onClose={() => setCheckoutModalOpen(false)}
          onCheckedOut={handleCheckedOut}
          asset={asset}
          reservationId={reservation.id}
        />
      )}
    </Stack>
  );
}
