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
  completed: "gray",
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
  // `completed` (product decision, bug fix): a reservation that already went
  // through one fulfil/return cycle mid-window can back a second checkout
  // while `now` is still inside its original window (server-enforced in
  // `CheckoutSerializer`, `RESERVATION_CHECKOUT_STATUSES`) — without this the
  // Check out button would vanish after the first return even though the API
  // still accepts it for the rest of the window.
  const isCheckoutRelevant =
    (reservation.status === "approved" || reservation.status === "completed") && (isOwn || canCheckout);

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

  // The server (`CheckoutSerializer.validate`, `backend/apps/reservations/
  // checkout.py`) now enforces that a reservation-backed checkout only
  // happens within the half-open window `[start_at, end_at)` — checking out
  // ahead of `start_at` or after `end_at` gets a 400, surfaced via
  // `rowError` above (docs/api-and-ui.md, docs/features.md). There is
  // currently no grace period past `end_at` (documented default, not yet a
  // confirmed product decision — see `docs/risks.md` §3). This button stays
  // visible AND enabled (never a hard client-side block, CLAUDE.md: UI
  // gating is convenience only, the server is the real boundary) — it is
  // only hinted outside the window below, since `now`/`isWithinReservation
  // Window` are computed once at render with nothing re-triggering a
  // re-render as time passes, and rely on the device's local clock, so a
  // hard `disabled` here could strand a `requires_approval` checkout (the
  // only checkout path for those categories) past the window's start with a
  // stale, un-clickable button.
  const now = Date.now();
  const isWithinReservationWindow =
    new Date(reservation.start_at).getTime() <= now && now < new Date(reservation.end_at).getTime();
  const showCheckoutButton = isCheckoutRelevant && !openCheckout && !!asset && !asset.is_consumable;
  const checkoutWindowHint = !isWithinReservationWindow
    ? now < new Date(reservation.start_at).getTime()
      ? "Checkout window hasn't started yet"
      : "Checkout window has ended"
    : null;
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
              title={checkoutWindowHint ?? undefined}
              onClick={() => setCheckoutModalOpen(true)}
              data-testid={`checkout-from-reservation-${reservation.id}`}
            >
              Check out
            </Button>
          )}
          {showCheckoutButton && checkoutWindowHint && (
            <Text size="xs" c="dimmed">
              {checkoutWindowHint}
            </Text>
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
