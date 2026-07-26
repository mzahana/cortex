import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Anchor,
  Button,
  FileButton,
  Group,
  Modal,
  NumberInput,
  Select,
  Stack,
  Text,
  Textarea,
  TextInput,
} from "@mantine/core";
import { DateInput } from "@mantine/dates";
import { useForm } from "@mantine/form";
import { api, ApiError } from "../../api/client";
import type { Asset, Expense, ExpenseCategory, ExpenseWritePayload } from "../../api/types";

interface ExpenseFormValues {
  category: string | null;
  amount: number | "";
  currency: string;
  date: Date | null;
  vendor: string;
  invoice_number: string;
  description: string;
  asset: string | null;
}

const EMPTY_VALUES: ExpenseFormValues = {
  category: null,
  amount: "",
  currency: "",
  date: new Date(),
  vendor: "",
  invoice_number: "",
  description: "",
  asset: null,
};

function toFormValues(expense: Expense): ExpenseFormValues {
  return {
    category: expense.category !== null ? String(expense.category) : null,
    amount: Number(expense.amount),
    currency: expense.currency,
    date: new Date(expense.date),
    vendor: expense.vendor,
    invoice_number: expense.invoice_number,
    description: expense.description,
    asset: expense.asset !== null ? String(expense.asset) : null,
  };
}

function toIsoDate(d: Date | null): string {
  if (!d) return "";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

interface ExpenseFormModalProps {
  opened: boolean;
  onClose: () => void;
  onSaved: (expense: Expense) => void;
  projectId: number;
  /** This project's own assets, for the optional "link to asset" picker
   * (already loaded by the parent tab — avoids a redundant fetch per open).
   * The server also accepts a general-pool (`project: null`) asset id, but
   * this picker only offers THIS project's own assets for simplicity — see
   * this file's own comment on `ExpenseSerializer.get_fields`'s wider
   * queryset for what the server would otherwise still accept if typed in
   * directly (not exposed here). */
  projectAssets: Asset[];
  /** The tenant's expense categories, for the category `<Select>` below
   * (already loaded by the parent tab — `ExpensesTab` — same "load once,
   * pass down" pattern as `projectAssets`; also used there to resolve the
   * ledger's `Expense.category` id to a name). */
  categories: ExpenseCategory[];
  /** True while `ExpensesTab` is still fetching `categories` — renders the
   * Select as a disabled "Loading categories…" placeholder instead of an
   * empty dropdown. */
  categoriesLoading: boolean;
  editing: Expense | null;
}

/**
 * Add/edit expense form (`docs/tasks/M7-project-grants.md`: "amount,
 * currency, date, category dropdown, vendor, invoice #, description,
 * optional asset link... invoice-scan upload"), gated by `expense.manage`
 * (caller-checked before this modal is ever opened — see `ExpensesTab`).
 *
 * The category field is a real `<Select>` fed from `GET /api/v1/
 * expense-categories` (a follow-up endpoint added after the M7 frontend
 * slice flagged its absence) — shows each category's name, submits its id.
 */
export function ExpenseFormModal({
  opened,
  onClose,
  onSaved,
  projectId,
  projectAssets,
  categories,
  categoriesLoading,
  editing,
}: ExpenseFormModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [savedExpense, setSavedExpense] = useState<Expense | null>(null);
  const resetFileRef = useRef<() => void>(null);

  const form = useForm<ExpenseFormValues>({
    initialValues: editing ? toFormValues(editing) : EMPTY_VALUES,
    validate: {
      amount: (v) => (v === "" || Number(v) <= 0 ? "Enter an amount greater than 0" : null),
      date: (v) => (v ? null : "Date is required"),
    },
  });

  useEffect(() => {
    if (!opened) return;
    setFormError(null);
    setUploadError(null);
    setSavedExpense(editing);
    form.setValues(editing ? toFormValues(editing) : EMPTY_VALUES);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened, editing]);

  const assetOptions = projectAssets.map((a) => ({ value: String(a.id), label: a.name }));
  // Active categories only for NEW selections — but if this expense already
  // points at a since-retired one (`is_active: false`), keep it selectable/
  // visible so editing doesn't silently blank out or hide the existing
  // choice (`ExpensesTab` fetches `include_inactive: true` for exactly this).
  const categoryOptions = categories
    .filter((c) => c.is_active || String(c.id) === (editing?.category ? String(editing.category) : null))
    .map((c) => ({ value: String(c.id), label: c.name }));

  const handleSubmit = async (values: ExpenseFormValues) => {
    setFormError(null);
    setSubmitting(true);
    try {
      const payload: ExpenseWritePayload = {
        category: values.category ? Number(values.category) : null,
        amount: String(values.amount),
        currency: values.currency.trim(),
        date: toIsoDate(values.date),
        vendor: values.vendor.trim(),
        invoice_number: values.invoice_number.trim(),
        description: values.description.trim(),
        asset: values.asset ? Number(values.asset) : null,
      };
      const expense = editing
        ? await api.updateExpense(editing.id, payload)
        : await api.createExpense(projectId, payload);
      setSavedExpense(expense);
      onSaved(expense);
      if (!editing) {
        // Stay open after a fresh create so the invoice-scan upload control
        // (which needs a real expense id) becomes available immediately,
        // matching the task's "add an expense with an invoice scan" flow as
        // one continuous action rather than a second "now edit it" step.
        return;
      }
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.problem.errors) {
        const { non_field_errors: nonField, ...fieldErrors } = err.problem.errors;
        if (Object.keys(fieldErrors).length > 0) {
          form.setErrors(
            Object.fromEntries(
              Object.entries(fieldErrors).map(([k, v]) => [
                k,
                Array.isArray(v) ? v.join(" ") : String(v),
              ]),
            ),
          );
        }
        if (nonField) setFormError(Array.isArray(nonField) ? nonField.join(" ") : String(nonField));
      } else if (err instanceof ApiError) {
        setFormError(err.problem.detail ?? err.problem.title);
      } else {
        setFormError("Unable to reach the server. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleUpload = async (file: File | null) => {
    if (!file || !savedExpense) return;
    setUploadError(null);
    setUploading(true);
    try {
      const attachment = await api.uploadExpenseAttachment(savedExpense.id, file, "doc");
      const updated = { ...savedExpense, attachments: [...savedExpense.attachments, attachment] };
      setSavedExpense(updated);
      onSaved(updated);
    } catch (err) {
      setUploadError(
        err instanceof ApiError ? err.problem.detail ?? err.problem.title : "Upload failed. Please try again.",
      );
    } finally {
      setUploading(false);
      resetFileRef.current?.();
    }
  };

  return (
    <Modal opened={opened} onClose={onClose} title={editing ? "Edit expense" : "New expense"} centered size="lg">
      <form onSubmit={form.onSubmit(handleSubmit)} noValidate>
        <Stack gap="sm">
          {formError && (
            <Alert color="red" data-testid="expense-form-error">
              {formError}
            </Alert>
          )}
          <Group grow>
            <NumberInput label="Amount" required decimalScale={2} min={0} {...form.getInputProps("amount")} />
            <TextInput label="Currency" placeholder="USD" {...form.getInputProps("currency")} />
          </Group>
          <Group grow>
            <DateInput label="Date" required {...form.getInputProps("date")} />
            <Select
              label="Category"
              placeholder={
                categoriesLoading
                  ? "Loading categories…"
                  : categoryOptions.length === 0
                    ? "No categories configured"
                    : "(uncategorized)"
              }
              data={categoryOptions}
              disabled={categoriesLoading}
              clearable
              searchable
              data-testid="expense-category-select"
              {...form.getInputProps("category")}
            />
          </Group>
          <Group grow>
            <TextInput label="Vendor" {...form.getInputProps("vendor")} />
            <TextInput label="Invoice #" {...form.getInputProps("invoice_number")} />
          </Group>
          <Select
            label="Link to asset (optional)"
            placeholder="(none — general expense)"
            data={assetOptions}
            clearable
            searchable
            {...form.getInputProps("asset")}
          />
          <Textarea label="Description" autosize minRows={2} {...form.getInputProps("description")} />

          <Button type="submit" loading={submitting} data-testid="expense-form-submit">
            {editing ? "Save changes" : "Create expense"}
          </Button>

          {savedExpense && (
            <Stack gap="xs" mt="sm">
              <Text size="sm" fw={600}>
                Invoice / receipt scans
              </Text>
              {uploadError && (
                <Alert color="red" data-testid="expense-attachment-upload-error">
                  {uploadError}
                </Alert>
              )}
              {savedExpense.attachments.length === 0 ? (
                <Text size="sm" c="dimmed">
                  No scans uploaded yet.
                </Text>
              ) : (
                <Stack gap={4}>
                  {savedExpense.attachments.map((att) => (
                    <Anchor
                      key={att.id}
                      href={`/media/${att.storage_key}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      size="sm"
                    >
                      {att.filename}
                    </Anchor>
                  ))}
                </Stack>
              )}
              <FileButton resetRef={resetFileRef} onChange={handleUpload} accept="image/*,.pdf,.doc,.docx">
                {(props) => (
                  <Button size="xs" variant="light" loading={uploading} data-testid="expense-upload-attachment" {...props}>
                    Upload invoice / receipt
                  </Button>
                )}
              </FileButton>
              <Button variant="default" onClick={onClose} data-testid="expense-form-done">
                Done
              </Button>
            </Stack>
          )}
        </Stack>
      </form>
    </Modal>
  );
}
