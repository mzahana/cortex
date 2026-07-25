import { useEffect, useState } from "react";
import { Alert, Button, Modal, Stack, Text, Textarea } from "@mantine/core";
import { DateTimePicker } from "@mantine/dates";
import { api, ApiError } from "../../api/client";
import type { Asset, Checkout } from "../../api/types";

interface CheckoutModalProps {
  opened: boolean;
  onClose: () => void;
  onCheckedOut: (checkout: Checkout) => void;
  asset: Asset;
  /** Links this checkout to an approved/fulfilled reservation belonging to
   * the caller for the same asset (reservation-first checkout,
   * `CheckoutCreatePayload.reservation` doc comment) — omit for a direct
   * walk-up checkout, same as before. */
  reservationId?: number;
}

/** Default due window: one week out, editable — matches the reservation
 * screen's "still fully editable, just pre-filled" convention. */
function defaultDueAt(): Date {
  const due = new Date();
  due.setDate(due.getDate() + 7);
  return due;
}

/**
 * Checkout form (T3.5, `POST /api/v1/checkouts`), reached from Asset Detail's
 * "Check out" action (direct walk-up, no `reservationId`) or from a
 * `ReservationListItem`'s "Check out" action (reservation-first — pre-filled
 * with `asset`/`reservationId`, post-MVP gap fill). A category configured
 * `requires_approval` rejects a reservation-less checkout with a clean `400`
 * (`apps.reservations.checkout.CheckoutSerializer.validate`) — surfaced
 * inline, telling the caller to reserve first, same "server is the
 * authority" pattern as `CreateReservationModal`'s 409 conflict handling.
 */
export function CheckoutModal({ opened, onClose, onCheckedOut, asset, reservationId }: CheckoutModalProps) {
  const [dueAt, setDueAt] = useState<Date | null>(defaultDueAt());
  const [condition, setCondition] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!opened) return;
    setDueAt(defaultDueAt());
    setCondition("");
    setFormError(null);
    setFieldErrors({});
  }, [opened]);

  const handleSubmit = async () => {
    setFormError(null);
    setFieldErrors({});

    if (!dueAt) {
      setFieldErrors({ due_at: "Choose a due date/time." });
      return;
    }
    if (dueAt <= new Date()) {
      setFieldErrors({ due_at: "Due date must be in the future." });
      return;
    }

    setSubmitting(true);
    try {
      const checkout = await api.createCheckout({
        asset: asset.id,
        due_at: dueAt.toISOString(),
        checkout_condition: condition.trim() || undefined,
        reservation: reservationId,
      });
      onCheckedOut(checkout);
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors) {
        const errors: Record<string, string> = {};
        for (const [k, v] of Object.entries(err.problem.errors)) {
          errors[k] = Array.isArray(v) ? v.join(" ") : String(v);
        }
        setFieldErrors(errors);
      } else if (err instanceof ApiError) {
        setFormError(err.problem.detail ?? err.problem.title);
      } else {
        setFormError("Unable to reach the server. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal opened={opened} onClose={onClose} title="Check out" centered>
      <Stack gap="sm">
        <Text size="sm" c="dimmed">
          {asset.name}
        </Text>

        {formError && (
          <Alert color="red" title="Couldn't check out" data-testid="checkout-form-error">
            {formError}
          </Alert>
        )}

        <DateTimePicker
          label="Due back"
          required
          value={dueAt}
          onChange={setDueAt}
          minDate={new Date()}
          error={fieldErrors.due_at}
          valueFormat="DD MMM YYYY, HH:mm"
        />

        <Textarea
          label="Condition notes"
          placeholder="Optional — condition at checkout"
          value={condition}
          onChange={(e) => setCondition(e.currentTarget.value)}
          error={fieldErrors.checkout_condition}
          autosize
          minRows={2}
        />

        {fieldErrors.reservation && (
          <Text size="sm" c="red">
            {fieldErrors.reservation}
          </Text>
        )}
        {fieldErrors.asset && (
          <Text size="sm" c="red">
            {fieldErrors.asset}
          </Text>
        )}

        <Button onClick={() => void handleSubmit()} loading={submitting} fullWidth mt="sm" data-testid="checkout-submit">
          Check out
        </Button>
      </Stack>
    </Modal>
  );
}
