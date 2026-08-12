/**
 * A project's expenses — its **orders** (M8 Phase 2, rev).
 *
 * The user's model: an expense IS an order from a vendor. One vendor, one
 * total that hits the bank, one or more shipments, items inside each. So this
 * tab lists orders, and expanding one shows what was in it. There is a single
 * "Add expense" button and a single form (`OrderFormModal`) behind it.
 *
 * This replaces an earlier cut that split the same information across two
 * workflows — an expense form here and a separate bank-charge screen — which
 * made the user assemble the structure by hand across screens. Whatever else
 * changes here, that must not come back.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Accordion,
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Group,
  Loader,
  Stack,
  Table,
  Text,
} from "@mantine/core";

import { api } from "../../api/client";
import { useAuth } from "../../hooks/useAuth";
import {
  EXPENSE_MANAGE,
  EXPENSE_VIEW,
  hasProjectScopedPermission,
} from "../../api/permissions";
import type { Asset, ExpenseCategory, Job, Order } from "../../api/types";
import { OrderFormModal } from "./OrderFormModal";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";

interface ExpensesTabProps {
  projectId: number;
}

function money(amount: string, currency: string): string {
  return `${currency} ${Number(amount).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function ExpensesTab({ projectId }: ExpensesTabProps) {
  const { me } = useAuth();
  const canView = hasProjectScopedPermission(me, EXPENSE_VIEW, projectId);
  const canManage = hasProjectScopedPermission(me, EXPENSE_MANAGE, projectId);

  const [orders, setOrders] = useState<Order[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [categories, setCategories] = useState<ExpenseCategory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Order | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Order | null>(null);
  /** Order id whose pack is currently rendering, so only that row shows a
   * spinner. */
  const [packBusy, setPackBusy] = useState<number | null>(null);
  const [packError, setPackError] = useState<string | null>(null);
  const [checklistBusy, setChecklistBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.listOrders(projectId);
      setOrders(page.results);
    } catch {
      setError("Could not load expenses.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (canView) void load();
    else setLoading(false);
  }, [canView, load]);

  useEffect(() => {
    // The form's asset picker offers this project's assets plus unassigned
    // ones — `?project=` cannot express "none", hence the two calls.
    Promise.all([
      api.listProjectAssets(projectId, { page_size: 200 }),
      api.listAssets({ unassigned: true, page_size: 200 }),
    ])
      .then(([own, pool]) => setAssets([...own.results, ...pool.results]))
      .catch(() => setAssets([]));
    api
      .listAllExpenseCategories({ include_inactive: true })
      .then(setCategories)
      .catch(() => setCategories([]));
  }, [projectId]);

  /** Ask for the pack, then poll the job until the PDF exists and open it.
   * Same async-job contract as the project report — the render merges every
   * scan and photo, so it cannot happen inside the request. */
  async function downloadPack(order: Order) {
    setPackBusy(order.id);
    setPackError(null);
    try {
      let job = await api.generateExpensePack(order.id);
      // Bounded poll (~60s): a pack with many scans takes a few seconds, and
      // an unbounded loop would spin forever on a job that failed to start.
      for (
        let attempt = 0;
        attempt < 30 && job.status !== "succeeded";
        attempt += 1
      ) {
        if (job.status === "failed")
          throw new Error(job.error || "render failed");
        await new Promise((resolve) => setTimeout(resolve, 2000));
        job = await api.getJob(job.id);
      }
      if (job.status !== "succeeded" || !job.download_url) {
        throw new Error("timed out");
      }
      window.open(job.download_url, "_blank", "noopener");
    } catch {
      setPackError("Could not build the expense pack. Try again in a moment.");
    } finally {
      setPackBusy(null);
    }
  }

  /** Poll a queued PDF job and open the result. Shared by both downloads —
   * they use the identical job contract. */
  async function pollAndOpen(job: Job): Promise<void> {
    for (
      let attempt = 0;
      attempt < 30 && job.status !== "succeeded";
      attempt += 1
    ) {
      if (job.status === "failed")
        throw new Error(job.error || "render failed");
      await new Promise((resolve) => setTimeout(resolve, 2000));
      job = await api.getJob(job.id);
    }
    if (job.status !== "succeeded" || !job.download_url)
      throw new Error("timed out");
    window.open(job.download_url, "_blank", "noopener");
  }

  async function downloadChecklist() {
    setChecklistBusy(true);
    setPackError(null);
    try {
      await pollAndOpen(await api.generateAuditChecklist(projectId));
    } catch {
      setPackError("Could not build the audit-readiness checklist.");
    } finally {
      setChecklistBusy(false);
    }
  }

  if (!canView) {
    return (
      <Alert color="gray" variant="light">
        You do not have access to this project's expenses.
      </Alert>
    );
  }

  return (
    <Stack gap="sm" data-testid="expenses-tab">
      <Group justify="space-between" align="center">
        <Text size="sm" c="dimmed">
          Each expense is one order from a vendor — what you paid, what arrived,
          and what was in it.
        </Text>
        <Group gap="xs">
          <Button
            size="xs"
            variant="light"
            loading={checklistBusy}
            onClick={() => void downloadChecklist()}
            data-testid="download-checklist"
          >
            Audit readiness
          </Button>
          {canManage && (
            <Button
              size="xs"
              onClick={() => {
                setEditing(null);
                setFormOpen(true);
              }}
              data-testid="add-expense"
            >
              Add expense
            </Button>
          )}
        </Group>
      </Group>

      {error && (
        <Alert color="red" variant="light">
          {error}
        </Alert>
      )}

      {packError && (
        <Alert color="red" variant="light">
          {packError}
        </Alert>
      )}

      {loading ? (
        <Loader size="sm" />
      ) : orders.length === 0 ? (
        <Card withBorder>
          <Text size="sm" c="dimmed">
            No expenses recorded for this project yet.
          </Text>
        </Card>
      ) : (
        <Accordion variant="separated">
          {orders.map((order) => {
            const itemCount = order.splits.reduce(
              (n, s) => n + s.items.length,
              0,
            );
            return (
              <Accordion.Item key={order.id} value={String(order.id)}>
                <Accordion.Control>
                  <Group justify="space-between" wrap="nowrap" pr="sm">
                    <Stack gap={0}>
                      <Text fw={600}>
                        {/* The same number the project report and the
                            downloaded audit pack use, so a row here can be
                            matched to either without opening them. */}
                        <Text span c="dimmed" fw={600} mr={6}>
                          #{order.number}
                        </Text>
                        {order.vendor || "Expense"} ·{" "}
                        {money(order.amount, order.currency)}
                      </Text>
                      <Text size="xs" c="dimmed">
                        {order.paid_on} · {itemCount} item
                        {itemCount === 1 ? "" : "s"}
                        {order.splits.length > 1
                          ? ` in ${order.splits.length} shipments`
                          : ""}
                      </Text>
                    </Stack>
                    {/* Amber means the items don't yet add up to what was
                        paid. A nudge, never a blocker. */}
                    <Badge
                      size="xs"
                      color={order.is_balanced ? "green" : "orange"}
                      variant="light"
                    >
                      {order.is_balanced
                        ? "Itemized"
                        : `${order.variance} unaccounted`}
                    </Badge>
                  </Group>
                </Accordion.Control>
                <Accordion.Panel>
                  <Stack gap="sm">
                    {order.splits.map((split, index) => (
                      <Stack gap={4} key={split.id ?? index}>
                        {order.splits.length > 1 && (
                          <Text size="xs" fw={600} c="dimmed">
                            Shipment {index + 1}
                            {split.receipt_number
                              ? ` · ${split.receipt_number}`
                              : ""}
                          </Text>
                        )}
                        <Table withRowBorders={false} verticalSpacing={4}>
                          <Table.Tbody>
                            {split.items.map((item) => (
                              <Table.Tr key={item.id}>
                                <Table.Td>{item.description || "—"}</Table.Td>
                                <Table.Td ta="right">
                                  {money(
                                    item.amount,
                                    split.currency || order.currency,
                                  )}
                                </Table.Td>
                              </Table.Tr>
                            ))}
                            {/* Shipping and tax get their OWN rows, not a
                                footnote: an auditor adds this column up, and a
                                charge buried in small print underneath makes
                                that impossible to follow. */}
                            {Number(split.shipping ?? 0) +
                              Number(split.tax ?? 0) >
                              0 && (
                              <>
                                <Table.Tr>
                                  <Table.Td c="dimmed">Shipping</Table.Td>
                                  <Table.Td ta="right" c="dimmed">
                                    {money(
                                      split.shipping ?? "0",
                                      split.currency || order.currency,
                                    )}
                                  </Table.Td>
                                </Table.Tr>
                                <Table.Tr>
                                  <Table.Td c="dimmed">Tax</Table.Td>
                                  <Table.Td ta="right" c="dimmed">
                                    {money(
                                      split.tax ?? "0",
                                      split.currency || order.currency,
                                    )}
                                  </Table.Td>
                                </Table.Tr>
                              </>
                            )}
                            <Table.Tr>
                              <Table.Td fw={600}>Receipt total</Table.Td>
                              <Table.Td ta="right" fw={600}>
                                {money(
                                  split.total ?? "0",
                                  split.currency || order.currency,
                                )}
                              </Table.Td>
                            </Table.Tr>
                          </Table.Tbody>
                        </Table>
                        {Number(split.shipping ?? 0) + Number(split.tax ?? 0) >
                          0 && (
                          <Text size="xs" c="dimmed">
                            Shipping and tax are spread across the items above
                            in proportion to price.
                          </Text>
                        )}
                        {split.currency &&
                          split.currency !== order.currency && (
                            <Text size="xs" c="dimmed">
                              Priced in {split.currency}; converted at the rate
                              your bank actually charged.
                            </Text>
                          )}
                      </Stack>
                    ))}

                    {order.attachments.map((att) => (
                      <Anchor
                        key={att.id}
                        href={`/media/${att.storage_key}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        size="xs"
                      >
                        Bank statement: {att.filename}
                      </Anchor>
                    ))}

                    <Group gap="xs">
                      <Button
                        size="compact-xs"
                        variant="light"
                        loading={packBusy === order.id}
                        onClick={() => void downloadPack(order)}
                        data-testid={`download-pack-${order.id}`}
                      >
                        Download audit pack
                      </Button>
                    </Group>

                    {canManage && (
                      <Group gap="xs">
                        <Button
                          size="compact-xs"
                          variant="light"
                          onClick={() => {
                            setEditing(order);
                            setFormOpen(true);
                          }}
                          data-testid={`edit-order-${order.id}`}
                        >
                          Edit
                        </Button>
                        <ActionIcon
                          size="sm"
                          variant="subtle"
                          color="red"
                          aria-label="Delete expense"
                          onClick={() => setDeleteTarget(order)}
                        >
                          ×
                        </ActionIcon>
                      </Group>
                    )}
                  </Stack>
                </Accordion.Panel>
              </Accordion.Item>
            );
          })}
        </Accordion>
      )}

      <OrderFormModal
        opened={formOpen}
        onClose={() => setFormOpen(false)}
        onSaved={() => void load()}
        projectId={projectId}
        assets={assets}
        categories={categories}
        editing={editing}
      />

      {deleteTarget && (
        <ConfirmDeleteModal
          opened={!!deleteTarget}
          title="Delete expense"
          itemLabel={`${deleteTarget.vendor || "expense"} — ${money(
            deleteTarget.amount,
            deleteTarget.currency,
          )}`}
          onClose={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await api.deleteOrder(deleteTarget.id);
          }}
          onDeleted={() => void load()}
        />
      )}
    </Stack>
  );
}
