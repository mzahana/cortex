import { useEffect, useState } from "react";
import { Alert, Button, Modal, NumberInput, Select, Stack, Text, TextInput } from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import type { StockItem, StockTxnReason, StockTxnResponse } from "../../api/types";

interface StockTxnModalProps {
  opened: boolean;
  onClose: () => void;
  /** Called with the server's response once the txn is applied — the
   * caller reconciles the displayed quantity from `stock_item.quantity_on_hand`
   * (the ledger-derived figure), not an optimistic client guess. */
  onApplied: (result: StockTxnResponse) => void;
  stockItem: StockItem;
  assetName: string;
  /** `"receive" | "consume"` open the modal pre-set to that reason (with the
   * sign implied and locked); `"adjust"` lets the user pick `adjust` or
   * `correction` and either sign, for corrections/reconciliation. */
  mode: "receive" | "consume" | "adjust";
}

type Direction = "increase" | "decrease";

interface FormValues {
  reason: StockTxnReason;
  direction: Direction;
  quantity: number | "";
  ref: string;
}

const MODE_TITLES: Record<StockTxnModalProps["mode"], string> = {
  receive: "Receive stock",
  consume: "Consume stock",
  adjust: "Adjust / correct stock",
};

/**
 * Receive/Consume/Adjust ledger-transaction form (T2.4). Submits
 * `POST /api/v1/stock/{id}/txn` with the right signed `delta`:
 * `receive` -> `+quantity`, `consume` -> `-quantity`, `adjust`/`correction`
 * -> the user-chosen sign. Client-side only validates `quantity > 0`
 * (CLAUDE.md: the server is authoritative) — a negative-result rejection
 * (400 RFC-7807) from the server is surfaced inline, not swallowed.
 */
export function StockTxnModal({
  opened,
  onClose,
  onApplied,
  stockItem,
  assetName,
  mode,
}: StockTxnModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm<FormValues>({
    initialValues: {
      reason: mode === "adjust" ? "adjust" : mode,
      direction: "increase",
      quantity: "",
      ref: "",
    },
    validate: {
      quantity: (value) =>
        value === "" || value === null || Number(value) <= 0
          ? "Quantity must be a positive number"
          : null,
    },
  });

  useEffect(() => {
    if (!opened) return;
    setFormError(null);
    form.setValues({
      reason: mode === "adjust" ? "adjust" : mode,
      direction: "increase",
      quantity: "",
      ref: "",
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened, mode, stockItem.id]);

  const handleSubmit = async (values: FormValues) => {
    setFormError(null);
    setSubmitting(true);
    try {
      const magnitude = Math.abs(Number(values.quantity));
      const delta =
        mode === "consume"
          ? -magnitude
          : mode === "receive"
            ? magnitude
            : values.direction === "decrease"
              ? -magnitude
              : magnitude;
      const result = await api.postStockTxn(stockItem.id, {
        reason: values.reason,
        delta,
        ref: values.ref.trim() || undefined,
      });
      onApplied(result);
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors) {
        form.setErrors(
          Object.fromEntries(
            Object.entries(err.problem.errors).map(([k, v]) => [
              k,
              Array.isArray(v) ? v.join(" ") : String(v),
            ]),
          ),
        );
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
    <Modal opened={opened} onClose={onClose} title={MODE_TITLES[mode]} centered>
      <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
        <Stack gap="sm">
          <Text size="sm" c="dimmed">
            {assetName} &middot; on hand: {stockItem.quantity_on_hand} {stockItem.unit_of_measure}
          </Text>

          {formError && (
            <Alert color="red" data-testid="stock-txn-form-error">
              {formError}
            </Alert>
          )}

          {mode === "adjust" && (
            <>
              <Select
                label="Type"
                data={[
                  { value: "adjust", label: "Adjust" },
                  { value: "correction", label: "Correction" },
                ]}
                value={form.values.reason}
                onChange={(value) => value && form.setFieldValue("reason", value as StockTxnReason)}
              />
              <Select
                label="Direction"
                data={[
                  { value: "increase", label: "Increase quantity" },
                  { value: "decrease", label: "Decrease quantity" },
                ]}
                value={form.values.direction}
                onChange={(value) => value && form.setFieldValue("direction", value as Direction)}
              />
            </>
          )}

          <NumberInput
            label="Quantity"
            required
            min={1}
            autoFocus
            {...form.getInputProps("quantity")}
          />

          <TextInput
            label="Reference"
            placeholder="PO number, note, etc. (optional)"
            {...form.getInputProps("ref")}
          />

          <Button type="submit" loading={submitting} fullWidth mt="sm" data-testid="stock-txn-submit">
            {mode === "receive" ? "Receive" : mode === "consume" ? "Consume" : "Apply"}
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}
