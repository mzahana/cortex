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
import { useCallback, useEffect, useRef, useState } from "react";
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
  TextInput,
} from "@mantine/core";
import { DateInput } from "@mantine/dates";
import { useDebouncedValue } from "@mantine/hooks";

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

/** Local calendar date as `YYYY-MM-DD` — NOT `toISOString()`, which shifts
 * to UTC and would move the boundary a day in a UTC+ timezone. */
function toIsoDate(d: Date | null): string | undefined {
  if (!d) return undefined;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

/** Strict `YYYY-MM-DD` for the date pickers. Mantine's default parser falls
 * back to `new Date(text)`, so half-typed input ("202", "2026-01") became a
 * real date and fired a filtered load per keystroke — one of them a 400. A
 * `null` here means "not a date yet", which `DateInput` ignores. */
function parseIsoDate(text: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text.trim());
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const date = new Date(y, mo - 1, d);
  return date.getFullYear() === y && date.getMonth() === mo - 1 && date.getDate() === d
    ? date
    : null;
}

function appendUnique(existing: Order[], incoming: Order[]): Order[] {
  const seen = new Set(existing.map((o) => o.id));
  return [...existing, ...incoming.filter((o) => !seen.has(o.id))];
}

export function ExpensesTab({ projectId }: ExpensesTabProps) {
  const { me } = useAuth();
  const canView = hasProjectScopedPermission(me, EXPENSE_VIEW, projectId);
  const canManage = hasProjectScopedPermission(me, EXPENSE_MANAGE, projectId);

  const [orders, setOrders] = useState<Order[]>([]);
  /** Total matching orders server-side, and the last page fetched — the list
   * is paged (100 per request) and "Show more" appends the next page, so a
   * project with more orders than one page still lists them all here. */
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebouncedValue(search.trim(), 300);
  /** Payment-date range (inclusive, either end optional) — filtered
   * server-side on `paid_on`, combined with the text search. */
  const [dateFrom, setDateFrom] = useState<Date | null>(null);
  const [dateTo, setDateTo] = useState<Date | null>(null);
  const dateFromIso = toIsoDate(dateFrom);
  const dateToIso = toIsoDate(dateTo);
  const filtered = Boolean(debouncedSearch || dateFromIso || dateToIso);
  /** Bumped per fresh load so a slow response for an older search term can't
   * overwrite the results of the newer one. */
  const loadSeq = useRef(0);
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

  /** Fetch pages 1..`pages` afresh. A new search starts at one page; a
   * reload after an edit/delete re-fetches as many pages as were already
   * showing, so the list doesn't collapse back to the first 100 under the
   * user. */
  const load = useCallback(
    async (pages = 1) => {
      const seq = ++loadSeq.current;
      setLoading(true);
      // A "Show more" for the previous filter may still be in flight; its
      // result is discarded (seq), so don't leave its spinner behind.
      setLoadingMore(false);
      setError(null);
      try {
        let rows: Order[] = [];
        let last: Awaited<ReturnType<typeof api.listOrders>> | null = null;
        for (let p = 1; p <= pages; p += 1) {
          last = await api.listOrders(projectId, {
            search: debouncedSearch || undefined,
            date_from: dateFromIso,
            date_to: dateToIso,
            page: p,
          });
          if (seq !== loadSeq.current) return;
          rows = appendUnique(rows, last.results);
          if (last.next === null) {
            pages = p;
            break;
          }
        }
        setOrders(rows);
        setTotal(last?.count ?? 0);
        setPage(pages);
        setHasMore(last?.next != null);
      } catch {
        if (seq === loadSeq.current) setError("Could not load expenses.");
      } finally {
        if (seq === loadSeq.current) setLoading(false);
      }
    },
    [projectId, debouncedSearch, dateFromIso, dateToIso],
  );

  async function loadMore() {
    const seq = loadSeq.current;
    setLoadingMore(true);
    setError(null);
    try {
      const next = await api.listOrders(projectId, {
        search: debouncedSearch || undefined,
        date_from: dateFromIso,
        date_to: dateToIso,
        page: page + 1,
      });
      if (seq !== loadSeq.current) return;
      // Offset paging: an order added/removed since page 1 shifts the rows,
      // so the boundary row can come back twice — never render it twice.
      setOrders((prev) => appendUnique(prev, next.results));
      setTotal(next.count);
      setPage(page + 1);
      setHasMore(next.next !== null);
    } catch {
      if (seq === loadSeq.current) setError("Could not load more expenses.");
    } finally {
      setLoadingMore(false);
    }
  }

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

      <TextInput
        placeholder="Search vendor, item, receipt #, order #, amount…"
        value={search}
        onChange={(e) => setSearch(e.currentTarget.value)}
        rightSection={
          search ? (
            <ActionIcon
              size="sm"
              variant="subtle"
              aria-label="Clear search"
              onClick={() => setSearch("")}
            >
              ×
            </ActionIcon>
          ) : null
        }
        data-testid="expense-search"
      />

      <Group grow gap="xs">
        <DateInput
          label="Paid from"
          placeholder="Any date"
          value={dateFrom}
          onChange={setDateFrom}
          maxDate={dateTo ?? undefined}
          valueFormat="YYYY-MM-DD"
          dateParser={parseIsoDate}
          clearable
          size="xs"
          data-testid="expense-date-from"
        />
        <DateInput
          label="Paid to"
          placeholder="Any date"
          value={dateTo}
          onChange={setDateTo}
          minDate={dateFrom ?? undefined}
          valueFormat="YYYY-MM-DD"
          dateParser={parseIsoDate}
          clearable
          size="xs"
          data-testid="expense-date-to"
        />
      </Group>

      {!loading && !error && (orders.length > 0 || filtered) && (
        <Text size="xs" c="dimmed" data-testid="expense-count">
          {filtered
            ? `${total} matching expense${total === 1 ? "" : "s"}`
            : `${total} expense${total === 1 ? "" : "s"}`}
          {orders.length < total ? ` · showing ${orders.length}` : ""}
        </Text>
      )}

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
            {filtered
              ? "No expenses match these filters."
              : "No expenses recorded for this project yet."}
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
                                  {/* A foreign-priced item also shows what it
                                      actually cost the project in the bank's
                                      currency — the figure the budget uses. */}
                                  {item.settled != null && (
                                    <Text
                                      size="xs"
                                      c="dimmed"
                                      data-testid={`item-settled-${item.id}`}
                                    >
                                      ≈ {money(item.settled, order.currency)}
                                    </Text>
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
                              your bank actually charged
                              {split.settled_total != null &&
                              split.fx_rate != null
                                ? ` (1 ${split.currency} = ${Number(split.fx_rate).toFixed(4)} ${order.currency}, receipt = ${money(split.settled_total, order.currency)})`
                                : ""}
                              .
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

      {!loading && hasMore && (
        <Button
          variant="light"
          size="xs"
          loading={loadingMore}
          onClick={() => void loadMore()}
          data-testid="expenses-load-more"
        >
          Show more ({total - orders.length} remaining)
        </Button>
      )}

      <OrderFormModal
        opened={formOpen}
        onClose={() => setFormOpen(false)}
        onSaved={() => void load(page)}
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
          onDeleted={() => void load(page)}
        />
      )}
    </Stack>
  );
}
