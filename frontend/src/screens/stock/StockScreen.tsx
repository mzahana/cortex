import { useEffect, useMemo, useState } from "react";
import {
  ActionIcon,
  Alert,
  AppShell,
  Button,
  Center,
  Group,
  Loader,
  Pagination,
  Stack,
  Switch,
  Tabs,
  Text,
  Title,
} from "@mantine/core";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import {
  hasAssetPermission,
  REORDER_REQUEST,
  STOCK_ADJUST,
  STOCK_CONSUME,
} from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import type { Location, StockItem, StockTxnResponse, ReorderRequest } from "../../api/types";
import { StockRow } from "./StockRows";
import { StockTxnModal } from "./StockTxnModal";
import { ReorderRequestModal } from "./ReorderRequestModal";
import { ReorderRequestsPanel } from "./ReorderRequestsPanel";
import { useStockList } from "./useStockList";

type TxnMode = "receive" | "consume" | "adjust";

/**
 * Stock / Consumables screen (T2.4, `docs/api-and-ui.md`: "Quantities,
 * low-stock highlights, receive/consume, reorder requests"). Server-side
 * paginated `GET /api/v1/stock` list with a low-stock toggle; receive/
 * consume/adjust actions post ledger transactions and reconcile the
 * displayed quantity from the server's response; a "Reorder requests" tab
 * drives the create + approve/transition workflow.
 */
export function StockScreen() {
  const navigate = useNavigate();
  const { me } = useAuth();

  const [lowStockOnly, setLowStockOnly] = useState(false);

  // NOTE: no `?search=` here — `StockItemViewSet` (`backend/apps/stock/
  // api.py`) only wires `DjangoFilterBackend`/`OrderingFilter`, no search
  // filter, and `docs/api-and-ui.md`'s Stock endpoint documents only the
  // low-stock filter. A search box here would silently do nothing server-
  // side (code-review finding, T2.4). Flagged as a follow-up below rather
  // than invented client-side.
  const filters = useMemo(
    () => ({ low_stock: lowStockOnly || undefined }),
    [lowStockOnly],
  );

  const { items, assetsById, totalCount, page, pageCount, loading, error, setPage, reload } =
    useStockList({ filters });

  // Bounded tenant config (bin locations) — same "resolve what's on screen"
  // reasoning as `AssetListScreen`'s category/location/project lookups.
  const [locations, setLocations] = useState<Location[]>([]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const locs = await api.listAllLocations({ ordering: "name" });
        if (!cancelled) setLocations(locs);
      } catch {
        // Non-fatal: bin location names just won't resolve; the row still
        // renders with the rest of its data.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  const locationNameById = useMemo(() => new Map(locations.map((l) => [l.id, l.name])), [locations]);

  const [txnTarget, setTxnTarget] = useState<{ stockItem: StockItem; mode: TxnMode } | null>(null);
  const [reorderTarget, setReorderTarget] = useState<StockItem | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  const handleTxnApplied = (result: StockTxnResponse) => {
    setBanner(
      `${result.txn.reason === "consume" ? "Consumed" : "Updated"} — now ${
        result.stock_item.quantity_on_hand
      } ${result.stock_item.unit_of_measure} on hand${result.low_stock ? " (low stock)" : ""}.`,
    );
    reload();
  };

  const handleReorderCreated = (request: ReorderRequest) => {
    setBanner(`Reorder request created (quantity ${request.quantity}).`);
    reload();
  };

  if (!me) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/")}>
              &#8592;
            </ActionIcon>
            <Title order={4}>Stock</Title>
          </Group>
          <Text size="sm" c="dimmed">
            {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
          </Text>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Tabs defaultValue="stock">
          <Tabs.List>
            <Tabs.Tab value="stock">Consumables</Tabs.Tab>
            <Tabs.Tab value="reorders">Reorder requests</Tabs.Tab>
          </Tabs.List>

          <Tabs.Panel value="stock" pt="md">
            <Stack gap="sm">
              {banner && (
                <Alert color="teal" withCloseButton onClose={() => setBanner(null)}>
                  {banner}
                </Alert>
              )}

              <Group>
                <Switch
                  label="Low stock only"
                  checked={lowStockOnly}
                  onChange={(e) => setLowStockOnly(e.currentTarget.checked)}
                  data-testid="low-stock-toggle"
                />
              </Group>

              {error && (
                <Alert color="red" title="Couldn't load stock">
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
                  <Loader data-testid="stock-list-loading" />
                </Center>
              )}

              {!loading && !error && items.length === 0 && (
                <Center p="xl">
                  <Stack align="center" gap={4}>
                    <Text fw={600}>
                      {lowStockOnly ? "Nothing is low on stock." : "No consumables tracked yet."}
                    </Text>
                    <Text size="sm" c="dimmed">
                      {lowStockOnly
                        ? "Nothing is currently at or below its reorder threshold."
                        : "Consumable assets get a stock item automatically once created."}
                    </Text>
                  </Stack>
                </Center>
              )}

              {!loading && !error && items.length > 0 && (
                <Stack gap="sm">
                  {items.map((stockItem) => {
                    const asset = assetsById.get(stockItem.asset);
                    const projectId = asset?.project ?? null;
                    return (
                      <StockRow
                        key={stockItem.id}
                        stockItem={stockItem}
                        asset={asset}
                        binLocationName={
                          stockItem.bin_location
                            ? locationNameById.get(stockItem.bin_location) ??
                              `Location #${stockItem.bin_location}`
                            : ""
                        }
                        canAdjust={hasAssetPermission(me, STOCK_ADJUST, projectId)}
                        canConsume={hasAssetPermission(me, STOCK_CONSUME, projectId)}
                        canRequestReorder={hasAssetPermission(me, REORDER_REQUEST, projectId)}
                        onReceive={() => setTxnTarget({ stockItem, mode: "receive" })}
                        onConsume={() => setTxnTarget({ stockItem, mode: "consume" })}
                        onAdjust={() => setTxnTarget({ stockItem, mode: "adjust" })}
                        onRequestReorder={() => setReorderTarget(stockItem)}
                      />
                    );
                  })}
                </Stack>
              )}

              {pageCount > 1 && (
                <Center>
                  <Pagination total={pageCount} value={page} onChange={setPage} size="sm" />
                </Center>
              )}
            </Stack>
          </Tabs.Panel>

          <Tabs.Panel value="reorders" pt="md">
            <ReorderRequestsPanel me={me} />
          </Tabs.Panel>
        </Tabs>
      </AppShell.Main>

      {txnTarget && (
        <StockTxnModal
          opened
          onClose={() => setTxnTarget(null)}
          onApplied={handleTxnApplied}
          stockItem={txnTarget.stockItem}
          assetName={assetsById.get(txnTarget.stockItem.asset)?.name ?? `Asset #${txnTarget.stockItem.asset}`}
          mode={txnTarget.mode}
        />
      )}

      {reorderTarget && (
        <ReorderRequestModal
          opened
          onClose={() => setReorderTarget(null)}
          onCreated={handleReorderCreated}
          stockItem={reorderTarget}
          assetName={assetsById.get(reorderTarget.asset)?.name ?? `Asset #${reorderTarget.asset}`}
        />
      )}
    </AppShell>
  );
}
