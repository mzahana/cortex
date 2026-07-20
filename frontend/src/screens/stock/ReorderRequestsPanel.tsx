import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  Pagination,
  Select,
  Stack,
  Table,
  Text,
} from "@mantine/core";
import { api, ApiError } from "../../api/client";
import { hasAssetPermission, REORDER_APPROVE } from "../../api/permissions";
import type { Asset, Me, ReorderRequest, ReorderRequestStatus, StockItem } from "../../api/types";
import { useReorderRequests } from "./useReorderRequests";

const STATUS_COLORS: Record<ReorderRequestStatus, string> = {
  open: "blue",
  approved: "grape",
  ordered: "orange",
  received: "green",
  cancelled: "gray",
};

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All statuses" },
  { value: "open", label: "Open" },
  { value: "approved", label: "Approved" },
  { value: "ordered", label: "Ordered" },
  { value: "received", label: "Received" },
  { value: "cancelled", label: "Cancelled" },
];

interface ReorderRequestsPanelProps {
  me: Me;
  /** Initial `?status=` filter (T3.4 Approvals screen reuses this panel
   * defaulted to `"open"` — the only status an approver can act on). Still
   * user-changeable via the status `Select` below. */
  defaultStatus?: string;
}

/**
 * Reorder-request lifecycle view (T2.4): server-paginated list, status
 * filter, and (for an approver, or the original requester where allowed)
 * the transition actions driving `PATCH /api/v1/reorder-requests/{id}`.
 * `docs/rbac.md`: `approved`/`ordered`/`received` require `reorder.approve`
 * (scoped to the request's asset's project); `cancelled` and plain edits
 * may also be done by the original requester. An invalid transition (400,
 * RFC-7807) is surfaced inline on that row, never thrown unhandled.
 */
export function ReorderRequestsPanel({ me, defaultStatus = "" }: ReorderRequestsPanelProps) {
  const [statusFilter, setStatusFilter] = useState<string>(defaultStatus);
  const filters = useMemo(
    () => ({ status: (statusFilter || undefined) as ReorderRequestStatus | undefined }),
    [statusFilter],
  );
  const { items, totalCount, page, pageCount, loading, error, setPage, reload } =
    useReorderRequests({ filters });

  // Resolve stock_item -> asset name for display (bounded to this page's
  // unique stock item ids — same "resolve what's on screen" approach as
  // `useStockList`, never a full stock/asset table load).
  const [stockItemsById, setStockItemsById] = useState<Map<number, StockItem>>(new Map());
  const [assetsById, setAssetsById] = useState<Map<number, Asset>>(new Map());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const missingStockItemIds = Array.from(new Set(items.map((r) => r.stock_item))).filter(
        (id) => !stockItemsById.has(id),
      );
      if (missingStockItemIds.length === 0) return;
      const fetchedStockItems = await Promise.all(
        missingStockItemIds.map(async (id) => {
          try {
            return await api.getStockItem(id);
          } catch {
            return null;
          }
        }),
      );
      if (cancelled) return;
      const nextStockItemsById = new Map(stockItemsById);
      for (const si of fetchedStockItems) if (si) nextStockItemsById.set(si.id, si);
      setStockItemsById(nextStockItemsById);

      const missingAssetIds = Array.from(
        new Set(fetchedStockItems.filter((si): si is StockItem => !!si).map((si) => si.asset)),
      ).filter((id) => !assetsById.has(id));
      if (missingAssetIds.length === 0) return;
      const fetchedAssets = await Promise.all(
        missingAssetIds.map(async (id) => {
          try {
            return await api.getAsset(id);
          } catch {
            return null;
          }
        }),
      );
      if (cancelled) return;
      const nextAssetsById = new Map(assetsById);
      for (const a of fetchedAssets) if (a) nextAssetsById.set(a.id, a);
      setAssetsById(nextAssetsById);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  const [rowBusy, setRowBusy] = useState<Record<number, boolean>>({});
  const [rowError, setRowError] = useState<Record<number, string>>({});

  const applyTransition = async (request: ReorderRequest, status: ReorderRequestStatus) => {
    setRowBusy((prev) => ({ ...prev, [request.id]: true }));
    setRowError((prev) => ({ ...prev, [request.id]: "" }));
    try {
      await api.updateReorderRequest(request.id, { status });
      reload();
    } catch (err) {
      if (err instanceof ApiError) {
        setRowError((prev) => ({
          ...prev,
          [request.id]: err.problem.detail ?? err.problem.title,
        }));
      } else {
        setRowError((prev) => ({
          ...prev,
          [request.id]: "Unable to reach the server. Please try again.",
        }));
      }
    } finally {
      setRowBusy((prev) => ({ ...prev, [request.id]: false }));
    }
  };

  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Select
          data={STATUS_OPTIONS}
          value={statusFilter}
          onChange={(v) => setStatusFilter(v ?? "")}
          w={200}
          aria-label="Filter by status"
        />
        <Text size="sm" c="dimmed">
          {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
        </Text>
      </Group>

      {error && (
        <Alert color="red" title="Couldn't load reorder requests">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{error}</Text>
            <Button size="xs" variant="light" onClick={reload}>
              Retry
            </Button>
          </Stack>
        </Alert>
      )}

      {loading && !error && (
        <Center p="xl">
          <Loader />
        </Center>
      )}

      {!loading && !error && items.length === 0 && (
        <Center p="xl">
          <Text c="dimmed">No reorder requests.</Text>
        </Center>
      )}

      {!loading && !error && items.length > 0 && (
        <Table.ScrollContainer minWidth={600}>
          <Table verticalSpacing="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Asset</Table.Th>
                <Table.Th>Qty</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Requested</Table.Th>
                <Table.Th>Actions</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((request) => {
                const stockItem = stockItemsById.get(request.stock_item);
                const asset = stockItem ? assetsById.get(stockItem.asset) : undefined;
                const projectId = asset?.project ?? null;
                const canApprove = hasAssetPermission(me, REORDER_APPROVE, projectId);
                const isRequester = request.requested_by === me.id;
                const busy = !!rowBusy[request.id];

                return (
                  <Table.Tr key={request.id}>
                    <Table.Td>{asset?.name ?? `Stock item #${request.stock_item}`}</Table.Td>
                    <Table.Td>{request.quantity}</Table.Td>
                    <Table.Td>
                      <Badge color={STATUS_COLORS[request.status]}>{request.status}</Badge>
                      {rowError[request.id] && (
                        <Text size="xs" c="red" mt={4}>
                          {rowError[request.id]}
                        </Text>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <Text size="xs" c="dimmed">
                        {new Date(request.requested_at).toLocaleDateString()}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Group gap={4}>
                        {request.status === "open" && canApprove && (
                          <Button
                            size="xs"
                            loading={busy}
                            onClick={() => void applyTransition(request, "approved")}
                          >
                            Approve
                          </Button>
                        )}
                        {request.status === "approved" && canApprove && (
                          <Button
                            size="xs"
                            loading={busy}
                            onClick={() => void applyTransition(request, "ordered")}
                          >
                            Mark ordered
                          </Button>
                        )}
                        {request.status === "ordered" && canApprove && (
                          <Button
                            size="xs"
                            loading={busy}
                            onClick={() => void applyTransition(request, "received")}
                          >
                            Mark received
                          </Button>
                        )}
                        {["open", "approved", "ordered"].includes(request.status) &&
                          (canApprove || isRequester) && (
                            <Button
                              size="xs"
                              color="red"
                              variant="light"
                              loading={busy}
                              onClick={() => void applyTransition(request, "cancelled")}
                            >
                              Cancel
                            </Button>
                          )}
                      </Group>
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}

      {pageCount > 1 && (
        <Center>
          <Pagination total={pageCount} value={page} onChange={setPage} size="sm" />
        </Center>
      )}
    </Stack>
  );
}
