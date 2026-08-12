/**
 * The ONE expense form (M8 Phase 2, rev).
 *
 * The user's model, in their words: *"the expense itself is the order made
 * from a vendor which can have splits and each split has items."* So this is a
 * single form for a single thing:
 *
 *     Vendor, date, total the bank took, bank statement
 *       └ Items (description, price, asset)
 *         └ Receipt for those items
 *       └ "Arrived in several shipments?" adds another receipt + its items
 *
 * **Splits are progressive disclosure.** A simple order shows no split UI at
 * all — just items and one receipt upload. The second shipment button is the
 * only thing that reveals the concept, and only for people whose order
 * actually arrived in pieces. Underneath, there is always at least one split,
 * so reconciliation and reporting have one code path.
 *
 * This replaces two disconnected workflows (an expense form here, a bank-charge
 * screen there) which required the user to assemble the structure by hand
 * across screens. Everything is one request: `api.createOrder` writes the
 * order, its splits and every item in one transaction, so a failure can never
 * leave a half-entered financial record.
 */
import { useEffect, useState } from "react";
import {
  ActionIcon,
  Alert,
  Anchor,
  Button,
  FileButton,
  Card,
  Divider,
  Group,
  Modal,
  Select,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { DateInput } from "@mantine/dates";

import { api, ApiError } from "../../api/client";
import type {
  Asset,
  ExpenseCategory,
  Order,
  OrderSplit,
} from "../../api/types";

interface DraftItem {
  id?: number;
  description: string;
  amount: string;
  category: string | null;
  /** ONE asset per item — an item is a single thing bought. */
  asset: string | null;
}

interface DraftSplit {
  id?: number;
  receipt_number: string;
  currency: string;
  shipping: string;
  tax: string;
  items: DraftItem[];
}

interface OrderFormModalProps {
  opened: boolean;
  onClose: () => void;
  onSaved: () => void;
  projectId: number;
  /** Assets this order's items may link: the project's own, plus unassigned. */
  assets: Asset[];
  categories: ExpenseCategory[];
  editing: Order | null;
}

const EMPTY_ITEM: DraftItem = {
  description: "",
  amount: "",
  category: null,
  asset: null,
};

function emptySplit(): DraftSplit {
  return {
    receipt_number: "",
    currency: "",
    shipping: "",
    tax: "",
    items: [{ ...EMPTY_ITEM }],
  };
}

function toIsoDate(d: Date | null): string {
  if (!d) return "";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

export function OrderFormModal({
  opened,
  onClose,
  onSaved,
  projectId,
  assets,
  categories,
  editing,
}: OrderFormModalProps) {
  const [vendor, setVendor] = useState("");
  const [paidOn, setPaidOn] = useState<Date | null>(new Date());
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("SAR");
  const [accountLabel, setAccountLabel] = useState("");
  const [statementRef, setStatementRef] = useState("");
  const [splits, setSplits] = useState<DraftSplit[]>([emptySplit()]);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The order as it exists on the server: the one being edited, or the one
   * just created. `null` until there is something to attach files to. */
  const [savedOrder, setSavedOrder] = useState<Order | null>(null);
  const [uploading, setUploading] = useState<string | null>(null);

  useEffect(() => {
    if (!opened) return;
    setError(null);
    setSavedOrder(editing);
    setUploading(null);
    if (editing) {
      setVendor(editing.vendor);
      setPaidOn(new Date(editing.paid_on));
      setAmount(editing.amount);
      setCurrency(editing.currency);
      setAccountLabel(editing.account_label);
      setStatementRef(editing.statement_ref);
      setSplits(
        editing.splits.map((s) => ({
          id: s.id,
          receipt_number: s.receipt_number ?? "",
          // Blank when it matches the order's currency, so the field stays
          // invisible for the ordinary same-currency case.
          currency:
            s.currency && s.currency !== editing.currency ? s.currency : "",
          shipping: s.shipping && Number(s.shipping) ? s.shipping : "",
          tax: s.tax && Number(s.tax) ? s.tax : "",
          items: s.items.map((i) => ({
            id: i.id,
            description: i.description,
            amount: i.amount,
            category: i.category ? String(i.category) : null,
            asset: i.asset != null ? String(i.asset) : null,
          })),
        })),
      );
    } else {
      setVendor("");
      setPaidOn(new Date());
      setAmount("");
      setCurrency("SAR");
      setAccountLabel("");
      setStatementRef("");
      setSplits([emptySplit()]);
    }
  }, [opened, editing]);

  const assetOptions = assets.map((a) => ({
    value: String(a.id),
    label: a.project === null ? `${a.name} (unassigned)` : a.name,
  }));
  const categoryOptions = categories
    .filter((c) => c.is_active)
    .map((c) => ({ value: String(c.id), label: c.name }));

  // The running tally: what the items add up to against what the bank took.
  // Warn, never block — real receipts carry roundings and partial refunds.
  const itemsTotal = splits.reduce(
    (sum, split) =>
      sum +
      split.items.reduce((s, i) => s + (Number(i.amount) || 0), 0) +
      (Number(split.shipping) || 0) +
      (Number(split.tax) || 0),
    0,
  );
  const paid = Number(amount) || 0;
  const anyForeign = splits.some(
    (s) => s.currency && s.currency.toUpperCase() !== currency,
  );
  const difference = Math.round((paid - itemsTotal) * 100) / 100;

  function updateSplit(index: number, patch: Partial<DraftSplit>) {
    setSplits((prev) =>
      prev.map((s, i) => (i === index ? { ...s, ...patch } : s)),
    );
  }

  function updateItem(
    splitIndex: number,
    itemIndex: number,
    patch: Partial<DraftItem>,
  ) {
    setSplits((prev) =>
      prev.map((s, i) =>
        i === splitIndex
          ? {
              ...s,
              items: s.items.map((it, j) =>
                j === itemIndex ? { ...it, ...patch } : it,
              ),
            }
          : s,
      ),
    );
  }

  async function uploadStatement(file: File | null) {
    if (!file || !savedOrder) return;
    setUploading("statement");
    setError(null);
    try {
      await api.uploadPaymentAttachment(savedOrder.id, file);
      // Re-fetch the whole order, not just the attachment: the file lists for
      // BOTH the statement and every receipt are rendered from `savedOrder`.
      setSavedOrder(await api.getOrder(savedOrder.id));
      onSaved();
    } catch {
      setError("Could not upload the bank statement.");
    } finally {
      setUploading(null);
    }
  }

  async function removeAttachment(
    kind: "statement" | "receipt",
    attachmentId: number,
  ) {
    if (!savedOrder) return;
    setUploading(`remove-${attachmentId}`);
    setError(null);
    try {
      if (kind === "statement") await api.deletePaymentAttachment(attachmentId);
      else await api.deletePurchaseAttachment(attachmentId);
      setSavedOrder(await api.getOrder(savedOrder.id));
      onSaved();
    } catch {
      setError("Could not remove that file.");
    } finally {
      setUploading(null);
    }
  }

  async function uploadReceipt(splitId: number | undefined, file: File | null) {
    if (!file || !splitId) return;
    setUploading(`receipt-${splitId}`);
    setError(null);
    try {
      await api.uploadPurchaseAttachment(splitId, file);
      if (savedOrder) setSavedOrder(await api.getOrder(savedOrder.id));
      onSaved();
    } catch {
      setError("Could not upload the receipt.");
    } finally {
      setUploading(null);
    }
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const payloadSplits: OrderSplit[] = splits.map((s) => ({
        id: s.id,
        receipt_number: s.receipt_number.trim(),
        currency: s.currency.trim().toUpperCase(),
        shipping: s.shipping.trim() || "0",
        tax: s.tax.trim() || "0",
        items: s.items
          .filter((i) => i.description.trim() || i.amount.trim())
          .map((i) => ({
            id: i.id,
            description: i.description.trim(),
            amount: i.amount.trim() || "0",
            category: i.category ? Number(i.category) : null,
            asset: i.asset ? Number(i.asset) : null,
          })),
      }));
      const payload = {
        project: projectId,
        vendor: vendor.trim(),
        paid_on: toIsoDate(paidOn),
        amount: amount.trim(),
        currency: currency.trim().toUpperCase(),
        account_label: accountLabel.trim(),
        statement_ref: statementRef.trim(),
        splits: payloadSplits,
      };
      const saved = editing
        ? await api.updateOrder(editing.id, payload)
        : await api.createOrder(payload);
      onSaved();
      // Attachments need real ids to hang off, which only exist after the
      // save. Rather than making the user reopen the expense to add its
      // paperwork, the form stays open and switches to the saved order — the
      // upload buttons below light up immediately.
      setSavedOrder(saved);
      // Adopt the server's ids so the per-shipment receipt buttons become
      // usable straight away (a freshly-created shipment had no id until now).
      setSplits(
        saved.splits.map((sp) => ({
          id: sp.id,
          receipt_number: sp.receipt_number ?? "",
          currency:
            sp.currency && sp.currency !== saved.currency ? sp.currency : "",
          shipping: sp.shipping && Number(sp.shipping) ? sp.shipping : "",
          tax: sp.tax && Number(sp.tax) ? sp.tax : "",
          items: sp.items.map((i) => ({
            id: i.id,
            description: i.description,
            amount: i.amount,
            category: i.category ? String(i.category) : null,
            asset: i.asset != null ? String(i.asset) : null,
          })),
        })),
      );
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not save this expense. Check the fields.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={editing ? "Edit expense" : "Add expense"}
      size="lg"
    >
      <Stack gap="sm">
        {error && (
          <Alert color="red" variant="light">
            {error}
          </Alert>
        )}

        <Group grow>
          <TextInput
            label="Vendor"
            placeholder="Amazon"
            value={vendor}
            onChange={(e) => setVendor(e.currentTarget.value)}
            data-testid="order-vendor"
          />
          <DateInput label="Date" value={paidOn} onChange={setPaidOn} />
        </Group>

        <Group grow>
          <TextInput
            label="Total paid"
            description="What your bank actually deducted"
            value={amount}
            onChange={(e) => setAmount(e.currentTarget.value)}
            data-testid="order-amount"
          />
          <TextInput
            label="Currency"
            maxLength={3}
            value={currency}
            onChange={(e) => setCurrency(e.currentTarget.value)}
          />
        </Group>

        <Group grow>
          <TextInput
            label="Card / account (optional)"
            placeholder="Visa •4321"
            value={accountLabel}
            onChange={(e) => setAccountLabel(e.currentTarget.value)}
          />
          <TextInput
            label="Statement reference (optional)"
            value={statementRef}
            onChange={(e) => setStatementRef(e.currentTarget.value)}
          />
        </Group>

        {/* Paperwork needs saved records to attach to, so these appear once
            the expense exists. On a NEW expense that is the moment you press
            Save — the form stays open rather than making you reopen it. */}
        <Group gap="xs" align="center">
          <FileButton
            onChange={(f) => void uploadStatement(f)}
            accept="application/pdf,image/jpeg,image/png,image/webp,image/heic,image/heif"
          >
            {(props) => (
              <Button
                {...props}
                size="xs"
                variant="light"
                disabled={!savedOrder}
                loading={uploading === "statement"}
                data-testid="upload-statement"
              >
                Attach bank statement
              </Button>
            )}
          </FileButton>
          {savedOrder?.attachments.map((att) => (
            <Group key={att.id} gap={4} wrap="nowrap">
              <Anchor
                href={`/media/${att.storage_key}`}
                target="_blank"
                rel="noopener noreferrer"
                size="xs"
              >
                {att.filename}
              </Anchor>
              <ActionIcon
                size="xs"
                variant="subtle"
                color="red"
                aria-label={`Remove ${att.filename}`}
                loading={uploading === `remove-${att.id}`}
                onClick={() => void removeAttachment("statement", att.id)}
              >
                ×
              </ActionIcon>
            </Group>
          ))}
          {!savedOrder && (
            <Text size="xs" c="dimmed">
              available once you save
            </Text>
          )}
        </Group>

        {splits.map((split, splitIndex) => (
          <Card
            withBorder
            key={split.id ?? splitIndex}
            data-testid={`order-split-${splitIndex}`}
          >
            <Stack gap="xs">
              {/* The word "shipment" only ever appears once there IS more than
                  one — a simple order shows a plain "Items" heading. */}
              <Group justify="space-between">
                <Text size="sm" fw={600}>
                  {splits.length > 1 ? `Shipment ${splitIndex + 1}` : "Items"}
                </Text>
                {splits.length > 1 && (
                  <ActionIcon
                    size="sm"
                    variant="subtle"
                    color="red"
                    aria-label={`Remove shipment ${splitIndex + 1}`}
                    onClick={() =>
                      setSplits((prev) =>
                        prev.filter((_, i) => i !== splitIndex),
                      )
                    }
                  >
                    ×
                  </ActionIcon>
                )}
              </Group>

              {split.items.map((item, itemIndex) => (
                <Group
                  key={item.id ?? itemIndex}
                  align="flex-end"
                  wrap="nowrap"
                  gap="xs"
                >
                  <TextInput
                    label={itemIndex === 0 ? "Item" : undefined}
                    placeholder="What was it?"
                    style={{ flex: 2 }}
                    value={item.description}
                    onChange={(e) =>
                      updateItem(splitIndex, itemIndex, {
                        description: e.currentTarget.value,
                      })
                    }
                    data-testid={`order-item-desc-${splitIndex}-${itemIndex}`}
                  />
                  <TextInput
                    label={itemIndex === 0 ? "Price" : undefined}
                    style={{ flex: 1 }}
                    value={item.amount}
                    onChange={(e) =>
                      updateItem(splitIndex, itemIndex, {
                        amount: e.currentTarget.value,
                      })
                    }
                    data-testid={`order-item-amount-${splitIndex}-${itemIndex}`}
                  />
                  <Select
                    label={itemIndex === 0 ? "Category" : undefined}
                    style={{ flex: 1 }}
                    data={categoryOptions}
                    value={item.category}
                    onChange={(v) =>
                      updateItem(splitIndex, itemIndex, { category: v })
                    }
                    clearable
                    searchable
                  />
                  <Select
                    label={itemIndex === 0 ? "Asset" : undefined}
                    placeholder="(none)"
                    style={{ flex: 2 }}
                    data={assetOptions}
                    value={item.asset}
                    onChange={(v) =>
                      updateItem(splitIndex, itemIndex, { asset: v })
                    }
                    clearable
                    searchable
                  />
                  <ActionIcon
                    variant="subtle"
                    color="red"
                    aria-label="Remove item"
                    onClick={() =>
                      setSplits((prev) =>
                        prev.map((s, i) =>
                          i === splitIndex
                            ? {
                                ...s,
                                items: s.items.filter(
                                  (_, j) => j !== itemIndex,
                                ),
                              }
                            : s,
                        ),
                      )
                    }
                  >
                    ×
                  </ActionIcon>
                </Group>
              ))}

              <Group gap="xs">
                <Button
                  size="compact-xs"
                  variant="light"
                  onClick={() =>
                    updateSplit(splitIndex, {
                      items: [...split.items, { ...EMPTY_ITEM }],
                    })
                  }
                  data-testid={`order-add-item-${splitIndex}`}
                >
                  + Add item
                </Button>
              </Group>

              <Group grow>
                <TextInput
                  size="xs"
                  label="Receipt #"
                  value={split.receipt_number}
                  onChange={(e) =>
                    updateSplit(splitIndex, {
                      receipt_number: e.currentTarget.value,
                    })
                  }
                />
                <TextInput
                  size="xs"
                  label="Shipping"
                  value={split.shipping}
                  onChange={(e) =>
                    updateSplit(splitIndex, { shipping: e.currentTarget.value })
                  }
                />
                <TextInput
                  size="xs"
                  label="Tax"
                  value={split.tax}
                  onChange={(e) =>
                    updateSplit(splitIndex, { tax: e.currentTarget.value })
                  }
                />
                <TextInput
                  size="xs"
                  label={`Priced in (if not ${currency})`}
                  placeholder={currency}
                  maxLength={3}
                  value={split.currency}
                  onChange={(e) =>
                    updateSplit(splitIndex, { currency: e.currentTarget.value })
                  }
                />
              </Group>
              <Group gap="xs" align="center">
                <FileButton
                  onChange={(f) => void uploadReceipt(split.id, f)}
                  accept="application/pdf,image/jpeg,image/png,image/webp,image/heic,image/heif"
                >
                  {(props) => (
                    <Button
                      {...props}
                      size="compact-xs"
                      variant="light"
                      disabled={!split.id}
                      loading={uploading === `receipt-${split.id}`}
                      data-testid={`upload-receipt-${splitIndex}`}
                    >
                      Attach receipt
                    </Button>
                  )}
                </FileButton>
                {!split.id && (
                  <Text size="xs" c="dimmed">
                    available once you save
                  </Text>
                )}
                {savedOrder?.splits
                  .find((sp) => sp.id === split.id)
                  ?.attachments?.map((att) => (
                    <Group key={att.id} gap={4} wrap="nowrap">
                      <Anchor
                        href={`/media/${att.storage_key}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        size="xs"
                      >
                        {att.filename}
                      </Anchor>
                      <ActionIcon
                        size="xs"
                        variant="subtle"
                        color="red"
                        aria-label={`Remove ${att.filename}`}
                        loading={uploading === `remove-${att.id}`}
                        onClick={() => void removeAttachment("receipt", att.id)}
                      >
                        ×
                      </ActionIcon>
                    </Group>
                  ))}
              </Group>

              {(Number(split.shipping) || Number(split.tax)) > 0 && (
                <Text size="xs" c="dimmed">
                  Shipping and tax are spread across these items by price.
                </Text>
              )}
            </Stack>
          </Card>
        ))}

        <Group>
          <Button
            size="xs"
            variant="subtle"
            onClick={() => setSplits((prev) => [...prev, emptySplit()])}
            data-testid="order-add-split"
          >
            + Order arrived in several shipments?
          </Button>
        </Group>

        <Divider />

        {/* The running tally. Suppressed when any shipment is priced in a
            foreign currency, where comparing raw numbers would be nonsense —
            the server works the rate out from the debit and shows it after
            saving. */}
        {anyForeign ? (
          <Text size="sm" c="dimmed" data-testid="order-tally">
            Items are priced in another currency. The exchange rate is worked
            out from what your bank actually took, and shown once you save.
          </Text>
        ) : (
          <Text
            size="sm"
            c={
              Math.abs(difference) < 0.005
                ? "green"
                : difference > 0
                  ? "orange"
                  : "red"
            }
            data-testid="order-tally"
          >
            {Math.abs(difference) < 0.005
              ? `Items add up to the ${currency} ${paid.toFixed(2)} you paid`
              : difference > 0
                ? `${currency} ${difference.toFixed(2)} of what you paid is not itemized yet`
                : `Items exceed what you paid by ${currency} ${Math.abs(difference).toFixed(2)}`}
          </Text>
        )}

        <Group justify="flex-end">
          <Button variant="subtle" onClick={onClose}>
            {savedOrder ? "Done" : "Cancel"}
          </Button>
          <Button
            loading={saving}
            onClick={() => void handleSave()}
            data-testid="order-save"
          >
            {savedOrder ? "Save changes" : "Save expense"}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
