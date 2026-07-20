import { useEffect, useState } from "react";
import { Alert, Button, Modal, NumberInput, Stack, Text, Textarea } from "@mantine/core";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import type { ReorderRequest, StockItem } from "../../api/types";

interface ReorderRequestModalProps {
  opened: boolean;
  onClose: () => void;
  onCreated: (request: ReorderRequest) => void;
  stockItem: StockItem;
  assetName: string;
}

interface FormValues {
  quantity: number | "";
  note: string;
}

/** "Request reorder" form (T2.4, `POST /api/v1/reorder-requests`) — pre-fills
 * quantity with the stock item's `reorder_target` (a sensible default, not
 * enforced) when it's above zero. */
export function ReorderRequestModal({
  opened,
  onClose,
  onCreated,
  stockItem,
  assetName,
}: ReorderRequestModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm<FormValues>({
    initialValues: { quantity: stockItem.reorder_target || "", note: "" },
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
    form.setValues({ quantity: stockItem.reorder_target || "", note: "" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened, stockItem.id]);

  const handleSubmit = async (values: FormValues) => {
    setFormError(null);
    setSubmitting(true);
    try {
      const created = await api.createReorderRequest({
        stock_item: stockItem.id,
        quantity: Number(values.quantity),
        note: values.note.trim() || undefined,
      });
      onCreated(created);
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
    <Modal opened={opened} onClose={onClose} title="Request reorder" centered>
      <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
        <Stack gap="sm">
          <Text size="sm" c="dimmed">
            {assetName} &middot; on hand: {stockItem.quantity_on_hand} {stockItem.unit_of_measure}
          </Text>

          {formError && (
            <Alert color="red" data-testid="reorder-form-error">
              {formError}
            </Alert>
          )}

          <NumberInput
            label="Quantity to order"
            required
            min={1}
            autoFocus
            {...form.getInputProps("quantity")}
          />

          <Textarea label="Note" placeholder="Optional" autosize minRows={2} {...form.getInputProps("note")} />

          <Button type="submit" loading={submitting} fullWidth mt="sm" data-testid="reorder-submit">
            Request reorder
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}
