import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  Pagination,
  Stack,
  Table,
  Text,
  Tooltip,
} from "@mantine/core";
import { IconLock } from "@tabler/icons-react";
import { api, ApiError } from "../../api/client";
import { EXPENSE_MANAGE, hasProjectScopedPermission } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import type { Asset, Expense, ExpenseCategory } from "../../api/types";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";
import { ExpenseFormModal } from "./ExpenseFormModal";

const PAGE_SIZE = 25;

interface ExpensesTabProps {
  projectId: number;
}

/**
 * Project hub — Expenses tab (`docs/tasks/M7-project-grants.md`: "paginated,
 * filterable... expense/invoice ledger"). Gated server-side by
 * project-scoped `expense.view` (`GET /projects/{id}/expenses`) — a caller
 * without it (Member/Viewer with no scoped Lead grant, or a Lead of a
 * DIFFERENT project) gets a 403 on the whole list, handled here as a normal
 * "no access" state (CLAUDE.md: "a 403 is a normal, handled outcome"), never
 * a crash. Add/edit is gated by `expense.manage`, presentation-only (the
 * server re-checks on every write).
 */
export function ExpensesTab({ projectId }: ExpensesTabProps) {
  const { me } = useAuth();
  const canManage = hasProjectScopedPermission(me, EXPENSE_MANAGE, projectId);

  const [items, setItems] = useState<Expense[]>([]);
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  // Bounded lookup of this project's own assets, for the expense form's
  // optional "link to asset" picker (reused across create/edit — see
  // `ExpenseFormModal`'s own comment on why it's scoped to this project only).
  const [projectAssets, setProjectAssets] = useState<Asset[]>([]);

  // The tenant's expense categories (`GET /api/v1/expense-categories`), for
  // the form's category `<Select>` AND for resolving each ledger row's bare
  // `Expense.category` id to a name below — loaded once here, same "load
  // once, pass/derive down" pattern as `projectAssets`.
  const [categories, setCategories] = useState<ExpenseCategory[]>([]);
  const [categoriesLoading, setCategoriesLoading] = useState(true);
  const [categoriesError, setCategoriesError] = useState<string | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Expense | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Expense | null>(null);

  const requestIdRef = useRef(0);

  const load = useCallback(
    async (targetPage: number) => {
      const requestId = ++requestIdRef.current;
      setLoading(true);
      setError(null);
      setForbidden(false);
      try {
        const body = await api.listProjectExpenses(projectId, {
          page: targetPage,
          page_size: PAGE_SIZE,
        });
        if (requestId !== requestIdRef.current) return;
        setItems(body.results);
        setTotalCount(body.count);
        setPage(targetPage);
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        if (err instanceof ApiError && err.isForbidden) {
          setForbidden(true);
          setError("You don't have access to this project's expenses.");
        } else {
          setError(
            err instanceof ApiError
              ? err.problem.detail ?? err.problem.title
              : "Unable to reach the server. Please try again.",
          );
        }
        setItems([]);
        setTotalCount(null);
      } finally {
        if (requestId === requestIdRef.current) setLoading(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    void load(1);
  }, [load]);

  useEffect(() => {
    api
      .listProjectAssets(projectId, { page_size: 100 })
      .then((body) => setProjectAssets(body.results))
      .catch(() => setProjectAssets([]));
  }, [projectId]);

  useEffect(() => {
    let cancelled = false;
    setCategoriesLoading(true);
    setCategoriesError(null);
    api
      // `include_inactive: true` here so an OLDER expense that references a
      // since-retired category still resolves to its real name in the
      // ledger below, not a bare id fallback — `ExpenseFormModal`'s own
      // Select filters back down to active-only for NEW selections.
      .listAllExpenseCategories({ ordering: "name", include_inactive: true })
      .then((rows) => {
        if (cancelled) return;
        setCategories(rows);
      })
      .catch((err) => {
        if (cancelled) return;
        setCategories([]);
        setCategoriesError(
          err instanceof ApiError
            ? `Categories couldn't load: ${err.problem.detail ?? err.problem.title}`
            : "Categories couldn't load (backend unreachable).",
        );
      })
      .finally(() => {
        if (!cancelled) setCategoriesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const categoryNameById = useMemo(
    () => new Map(categories.map((c) => [c.id, c.name])),
    [categories],
  );

  const openCreate = () => {
    setEditing(null);
    setFormOpen(true);
  };

  const openEdit = (expense: Expense) => {
    setEditing(expense);
    setFormOpen(true);
  };

  return (
    <Stack gap="sm" data-testid="expenses-tab">
      <Group justify="space-between">
        <Text size="sm" c="dimmed">
          {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
        </Text>
        {canManage && (
          <Button size="xs" onClick={openCreate} data-testid="new-expense-button">
            Add expense
          </Button>
        )}
      </Group>

      {categoriesError && (
        <Alert color="yellow" data-testid="expense-categories-error">
          {categoriesError}
        </Alert>
      )}

      {error && (
        <Alert
          color={forbidden ? "gray" : "red"}
          icon={forbidden ? <IconLock size={16} /> : undefined}
          title={forbidden ? "Not available" : "Couldn't load expenses"}
          data-testid="expenses-error"
        >
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{error}</Text>
            {!forbidden && (
              <Button size="xs" variant="light" onClick={() => load(page)}>
                Retry
              </Button>
            )}
          </Stack>
        </Alert>
      )}

      {loading && !error && (
        <Center p="xl">
          <Loader data-testid="expenses-loading" />
        </Center>
      )}

      {!loading && !error && items.length === 0 && (
        <Center p="xl">
          <Text c="dimmed">No expenses recorded yet.</Text>
        </Center>
      )}

      {!loading && !error && items.length > 0 && (
        <Table.ScrollContainer minWidth={640}>
          <Table verticalSpacing="xs" data-testid="expenses-table">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Date</Table.Th>
                <Table.Th>Amount</Table.Th>
                <Table.Th>Vendor</Table.Th>
                <Table.Th>Invoice #</Table.Th>
                <Table.Th>Category</Table.Th>
                <Table.Th>Scans</Table.Th>
                {canManage && <Table.Th />}
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((expense) => (
                <Table.Tr key={expense.id} data-testid={`expense-row-${expense.id}`}>
                  <Table.Td>{expense.date}</Table.Td>
                  <Table.Td>
                    {expense.currency ? `${expense.currency} ` : ""}
                    {expense.amount}
                  </Table.Td>
                  <Table.Td>{expense.vendor || <Text c="dimmed">—</Text>}</Table.Td>
                  <Table.Td>{expense.invoice_number || <Text c="dimmed">—</Text>}</Table.Td>
                  <Table.Td>
                    {expense.category !== null ? (
                      <Badge variant="light">
                        {categoryNameById.get(expense.category) ?? `Category #${expense.category}`}
                      </Badge>
                    ) : (
                      <Text c="dimmed">—</Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    {expense.attachments.length === 0 ? (
                      <Text c="dimmed">—</Text>
                    ) : (
                      <Group gap={4}>
                        {expense.attachments.map((att) => (
                          <Anchor
                            key={att.id}
                            href={`/media/${att.storage_key}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            size="xs"
                          >
                            {att.filename}
                          </Anchor>
                        ))}
                      </Group>
                    )}
                  </Table.Td>
                  {canManage && (
                    <Table.Td>
                      <Group gap={4} justify="flex-end" wrap="nowrap">
                        <Tooltip label="Edit">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            aria-label={`Edit expense ${expense.id}`}
                            onClick={() => openEdit(expense)}
                          >
                            ✎
                          </ActionIcon>
                        </Tooltip>
                        <Tooltip label="Delete">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            color="red"
                            aria-label={`Delete expense ${expense.id}`}
                            onClick={() => setDeleteTarget(expense)}
                          >
                            🗑
                          </ActionIcon>
                        </Tooltip>
                      </Group>
                    </Table.Td>
                  )}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}

      {totalCount !== null && totalCount > PAGE_SIZE && (
        <Center>
          <Pagination
            total={Math.max(1, Math.ceil(totalCount / PAGE_SIZE))}
            value={page}
            onChange={(p) => void load(p)}
            size="sm"
          />
        </Center>
      )}

      <ExpenseFormModal
        opened={formOpen}
        onClose={() => setFormOpen(false)}
        onSaved={() => void load(page)}
        projectId={projectId}
        projectAssets={projectAssets}
        categories={categories}
        categoriesLoading={categoriesLoading}
        editing={editing}
      />

      {deleteTarget && (
        <ConfirmDeleteModal
          opened={!!deleteTarget}
          title="Delete expense"
          itemLabel={`${deleteTarget.currency} ${deleteTarget.amount} — ${deleteTarget.vendor || "expense"}`}
          onClose={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await api.deleteExpense(deleteTarget.id);
          }}
          onDeleted={() => void load(page)}
        />
      )}
    </Stack>
  );
}
