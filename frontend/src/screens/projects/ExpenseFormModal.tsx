import { useEffect, useRef, useState } from "react";
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
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
import { IconTrash } from "@tabler/icons-react";
import { DOC_TYPE_OPTIONS } from "../assets/PhotoCapture";
import { api, ApiError } from "../../api/client";

/** Same labels the asset screen shows, derived from the one exported list so
 * the two can't drift. */
const DOC_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  DOC_TYPE_OPTIONS.filter((o) => o.value).map((o) => [o.value, o.label]),
);
import type { ExpenseAttachment } from "../../api/types";
import type {
  Asset,
  AssetExpensePrefill,
  Expense,
  ExpenseCategory,
  ExpenseWritePayload,
} from "../../api/types";

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
  const [removingAttachmentId, setRemovingAttachmentId] = useState<number | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [savedExpense, setSavedExpense] = useState<Expense | null>(null);
  const resetFileRef = useRef<() => void>(null);
  // "Fetch from asset" (convenience only — every field stays editable, and
  // the user can ignore this entirely and type everything by hand).
  const [prefill, setPrefill] = useState<AssetExpensePrefill | null>(null);
  const [prefilling, setPrefilling] = useState(false);
  const [prefillError, setPrefillError] = useState<string | null>(null);
  const [copyingDocId, setCopyingDocId] = useState<number | null>(null);
  /** Documents queued to copy onto the expense. Seeded from the server's own
   * ranking (`documents[0]`, financial types first) so "fetch from asset"
   * brings the invoice across by default — the previous cut required a
   * separate manual click AFTER saving, which is why the invoice kept not
   * arriving. */
  const [selectedDocIds, setSelectedDocIds] = useState<Set<number>>(new Set());

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
    setRemoveError(null);
    setSavedExpense(editing);
    setPrefill(null);
    setPrefillError(null);
    setSelectedDocIds(new Set());
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

  /** Pull the linked asset's own purchase facts into the form. Only fills
   * fields the user hasn't already typed into — fetching must never silently
   * overwrite something they entered by hand. The date is the one exception
   * on a NEW expense: it defaults to today (never blank), so "blank-only"
   * would make it unfillable, and the asset's purchase date is the whole
   * point of fetching. On an EDIT, the stored date is real user data and is
   * left alone. */
  const handleFetchFromAsset = async () => {
    const assetId = form.values.asset;
    if (!assetId) return;
    setPrefilling(true);
    setPrefillError(null);
    try {
      const data = await api.getAssetExpensePrefill(Number(assetId));
      setPrefill(data);
      // Preselect the best candidate the server ranked (an invoice/receipt/PO
      // if one is tagged). Nothing tagged financial -> nothing preselected,
      // so we never silently attach an unrelated photo.
      const best = data.documents.find((doc) => doc.is_financial);
      setSelectedDocIds(best ? new Set([best.id]) : new Set());
      const next: Partial<ExpenseFormValues> = {};
      if (form.values.amount === "" && data.amount) next.amount = Number(data.amount);
      if (!form.values.currency.trim() && data.currency) next.currency = data.currency;
      if (!form.values.vendor.trim() && data.vendor) next.vendor = data.vendor;
      if (!editing && data.date) next.date = new Date(`${data.date}T00:00:00`);
      if (!form.values.description.trim() && data.description) {
        next.description = data.description;
      }
      form.setValues((current) => ({ ...current, ...next }));
    } catch (err) {
      setPrefillError(
        err instanceof ApiError
          ? (err.problem.detail ?? err.problem.title)
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setPrefilling(false);
    }
  };

  /** Copy one of the asset's documents onto an already-saved expense. */
  const handleCopyDocument = async (attachmentId: number) => {
    if (!savedExpense) return;
    setCopyingDocId(attachmentId);
    setUploadError(null);
    try {
      const attachment = await api.copyAssetAttachmentToExpense(savedExpense.id, attachmentId);
      setSavedExpense({
        ...savedExpense,
        attachments: [...(savedExpense.attachments ?? []), attachment],
      });
      setSelectedDocIds((prev) => {
        const next = new Set(prev);
        next.delete(attachmentId);
        return next;
      });
    } catch (err) {
      setUploadError(
        err instanceof ApiError
          ? (err.problem.detail ?? err.problem.title)
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setCopyingDocId(null);
    }
  };

  /** Copy every ticked asset document onto a freshly-saved expense.
   * Deliberately runs AFTER the expense exists (the copy endpoint needs its
   * id) but without making the user click again — the expense itself is
   * already saved either way, so a copy failure surfaces as a banner rather
   * than failing the save. */
  const copyQueuedDocuments = async (expenseId: number, expense: Expense): Promise<Expense> => {
    if (selectedDocIds.size === 0) return expense;
    const copied = [];
    const failures: string[] = [];
    for (const attachmentId of selectedDocIds) {
      try {
        copied.push(await api.copyAssetAttachmentToExpense(expenseId, attachmentId));
      } catch (err) {
        failures.push(
          err instanceof ApiError
            ? (err.problem.detail ?? err.problem.title)
            : "Unable to reach the server.",
        );
      }
    }
    if (failures.length > 0) {
      setUploadError(`Couldn't copy ${failures.length} document(s): ${failures.join("; ")}`);
    }
    setSelectedDocIds(new Set());
    return { ...expense, attachments: [...(expense.attachments ?? []), ...copied] };
  };

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
      const saved = editing
        ? await api.updateExpense(editing.id, payload)
        : await api.createExpense(projectId, payload);
      // Bring across any asset documents the user ticked when they fetched
      // from the asset — this is what makes "fetch from asset" actually
      // deliver the invoice instead of just its metadata.
      const expense = await copyQueuedDocuments(saved.id, saved);
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
      const kind = file.type.startsWith("image/") ? "photo" : "doc";
      const attachment = await api.uploadExpenseAttachment(savedExpense.id, file, kind);
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

  const handleRemoveAttachment = async (attachment: ExpenseAttachment) => {
    if (!savedExpense) return;
    setRemoveError(null);
    setRemovingAttachmentId(attachment.id);
    try {
      await api.deleteExpenseAttachment(attachment.id);
      const updated = {
        ...savedExpense,
        attachments: savedExpense.attachments.filter((a) => a.id !== attachment.id),
      };
      setSavedExpense(updated);
      onSaved(updated);
    } catch (err) {
      setRemoveError(
        err instanceof ApiError ? err.problem.detail ?? err.problem.title : "Remove failed. Please try again.",
      );
    } finally {
      setRemovingAttachmentId(null);
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

          {form.values.asset && (
            <Stack gap="xs">
              <Group gap="xs">
                <Button
                  size="xs"
                  variant="light"
                  loading={prefilling}
                  onClick={() => void handleFetchFromAsset()}
                  data-testid="expense-fetch-from-asset"
                >
                  Fetch details from asset
                </Button>
                <Text size="xs" c="dimmed">
                  Fills the fields you have left blank{editing ? "" : ", plus the date"}
                </Text>
              </Group>
              {prefillError && (
                <Alert color="red" data-testid="expense-prefill-error">
                  {prefillError}
                </Alert>
              )}
              {prefill && prefill.documents.length > 0 && (
                <Stack gap={4}>
                  <Text size="xs" fw={600}>
                    Documents on this asset
                  </Text>
                  <Text size="xs" c="dimmed">
                    Ticked documents are copied onto the expense when you save.
                  </Text>
                  {prefill.documents.map((doc) => {
                    const alreadyCopied = (savedExpense?.attachments ?? []).some(
                      (att) => att.filename === doc.filename,
                    );
                    return (
                      <Group key={doc.id} gap="xs" wrap="nowrap">
                        <Checkbox
                          size="xs"
                          checked={selectedDocIds.has(doc.id)}
                          disabled={alreadyCopied}
                          onChange={() =>
                            setSelectedDocIds((prev) => {
                              const next = new Set(prev);
                              if (next.has(doc.id)) next.delete(doc.id);
                              else next.add(doc.id);
                              return next;
                            })
                          }
                          aria-label={`Copy ${doc.filename} to this expense`}
                          data-testid={`expense-select-asset-doc-${doc.id}`}
                        />
                        <Text size="xs" style={{ flex: 1, minWidth: 0 }} truncate>
                          {doc.filename}
                        </Text>
                        {doc.doc_type && (
                          <Badge size="xs" variant="light" color={doc.is_financial ? "teal" : "gray"}>
                            {DOC_TYPE_LABELS[doc.doc_type] ?? doc.doc_type}
                          </Badge>
                        )}
                        {savedExpense && !alreadyCopied && (
                          <Button
                            size="compact-xs"
                            variant="subtle"
                            loading={copyingDocId === doc.id}
                            onClick={() => void handleCopyDocument(doc.id)}
                            data-testid={`expense-copy-asset-doc-${doc.id}`}
                          >
                            Attach now
                          </Button>
                        )}
                        {alreadyCopied && (
                          <Text size="xs" c="dimmed">
                            Attached
                          </Text>
                        )}
                      </Group>
                    );
                  })}
                </Stack>
              )}
            </Stack>
          )}
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
              {removeError && (
                <Alert color="red" data-testid="expense-attachment-remove-error">
                  {removeError}
                </Alert>
              )}
              {savedExpense.attachments.length === 0 ? (
                <Text size="sm" c="dimmed">
                  No scans uploaded yet.
                </Text>
              ) : (
                <Stack gap={4}>
                  {savedExpense.attachments.map((att) => (
                    <Group key={att.id} gap="xs" justify="space-between" wrap="nowrap">
                      <Anchor
                        href={`/media/${att.storage_key}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        size="sm"
                      >
                        {att.filename}
                      </Anchor>
                      <ActionIcon
                        variant="subtle"
                        color="red"
                        size="sm"
                        aria-label="Remove attachment"
                        loading={removingAttachmentId === att.id}
                        onClick={() => handleRemoveAttachment(att)}
                        data-testid={`expense-attachment-remove-${att.id}`}
                      >
                        <IconTrash size={14} />
                      </ActionIcon>
                    </Group>
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
