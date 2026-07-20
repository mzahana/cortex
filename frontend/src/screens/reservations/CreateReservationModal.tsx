import { useEffect, useState } from "react";
import { Alert, Button, Modal, Stack, Text } from "@mantine/core";
import { DateTimePicker } from "@mantine/dates";
import { api, ApiError } from "../../api/client";
import type { Asset, Reservation } from "../../api/types";
import { AssetPickerSelect } from "./AssetPickerSelect";

interface CreateReservationModalProps {
  opened: boolean;
  onClose: () => void;
  onCreated: (reservation: Reservation, asset: Asset) => void;
  /** Pre-filled window (e.g. the day currently selected on the calendar) —
   * still fully editable. */
  initialStart?: Date;
  initialEnd?: Date;
  initialAsset?: Asset | null;
}

/**
 * Create-reservation form (T3.4, `POST /api/v1/reservations`). Live conflict
 * feedback: an overlapping window 409s (`ReservationConflict`,
 * `apps.reservations.services`) and is surfaced inline as a clean "already
 * reserved" message, never a raw failure — same for the 400s the service
 * raises for the lead-time/duration/per-user-cap checks (F4 acceptance).
 */
export function CreateReservationModal({
  opened,
  onClose,
  onCreated,
  initialStart,
  initialEnd,
  initialAsset,
}: CreateReservationModalProps) {
  const [asset, setAsset] = useState<Asset | null>(initialAsset ?? null);
  const [start, setStart] = useState<Date | null>(initialStart ?? null);
  const [end, setEnd] = useState<Date | null>(initialEnd ?? null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!opened) return;
    setAsset(initialAsset ?? null);
    setStart(initialStart ?? null);
    setEnd(initialEnd ?? null);
    setFormError(null);
    setConflict(false);
    setFieldErrors({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);

  const handleSubmit = async () => {
    setFormError(null);
    setConflict(false);
    setFieldErrors({});

    if (!asset) {
      setFieldErrors({ asset: "Choose an asset to reserve." });
      return;
    }
    if (!start || !end) {
      setFieldErrors({ end_at: "Choose a start and end time." });
      return;
    }
    if (end <= start) {
      setFieldErrors({ end_at: "End time must be after the start time." });
      return;
    }

    setSubmitting(true);
    try {
      const reservation = await api.createReservation({
        asset: asset.id,
        start_at: start.toISOString(),
        end_at: end.toISOString(),
      });
      onCreated(reservation, asset);
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // F4 conflict — the asset is already booked for an overlapping
        // active reservation. Clean inline feedback, not a raw error banner.
        setConflict(true);
        setFormError(err.problem.detail ?? "This asset is already reserved for that time window.");
      } else if (err instanceof ApiError && err.problem.errors) {
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
    <Modal opened={opened} onClose={onClose} title="Reserve an asset" centered>
      <Stack gap="sm">
        {formError && (
          <Alert color={conflict ? "orange" : "red"} title={conflict ? "Time conflict" : "Couldn't create reservation"} data-testid="reservation-form-error">
            {formError}
          </Alert>
        )}

        <AssetPickerSelect
          label="Asset"
          placeholder="Search assets…"
          required
          value={asset}
          onChange={setAsset}
          error={fieldErrors.asset}
        />

        <DateTimePicker
          label="Start"
          required
          value={start}
          onChange={setStart}
          minDate={new Date()}
          error={fieldErrors.start_at}
          valueFormat="DD MMM YYYY, HH:mm"
        />

        <DateTimePicker
          label="End"
          required
          value={end}
          onChange={setEnd}
          minDate={start ?? new Date()}
          error={fieldErrors.end_at}
          valueFormat="DD MMM YYYY, HH:mm"
        />

        {fieldErrors.non_field_errors && (
          <Text size="sm" c="red">
            {fieldErrors.non_field_errors}
          </Text>
        )}

        <Button onClick={handleSubmit} loading={submitting} fullWidth mt="sm" data-testid="reservation-submit">
          Reserve
        </Button>
      </Stack>
    </Modal>
  );
}
