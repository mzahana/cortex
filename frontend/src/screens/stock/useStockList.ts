import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { Asset, StockItem, StockListParams } from "../../api/types";

const PAGE_SIZE = 25;

interface UseStockListOptions {
  /** Caller-owned filter object — `useMemo`'d by value so this hook only
   * refetches when the actual filter *content* changes (same pattern as
   * `useAssetList`). */
  filters: StockListParams;
}

interface UseStockListResult {
  items: StockItem[];
  /** `Asset.id -> Asset`, resolved for every stock item currently on the
   * page (`StockItem.asset` is a plain id, not nested — see `StockItem`
   * type doc comment). Bounded to the current page size (<= 25 lookups),
   * never a full asset-table load. */
  assetsById: Map<number, Asset>;
  totalCount: number | null;
  page: number;
  pageCount: number;
  loading: boolean;
  error: string | null;
  setPage: (page: number) => void;
  reload: () => void;
}

/**
 * Server-side paginated stock list (T2.4, `GET /api/v1/stock`). One bounded
 * page at a time — never "load all stock" (CLAUDE.md). A filter change
 * (search/low-stock toggle/ordering) resets to page 1.
 */
export function useStockList({ filters }: UseStockListOptions): UseStockListResult {
  const [items, setItems] = useState<StockItem[]>([]);
  const [assetsById, setAssetsById] = useState<Map<number, Asset>>(new Map());
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [page, setPageState] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const requestIdRef = useRef(0);
  const filtersKey = JSON.stringify(filters);

  const fetchPage = useCallback(
    async (targetPage: number) => {
      const requestId = ++requestIdRef.current;
      setLoading(true);
      setError(null);
      try {
        const body = await api.listStock({ ...filters, page: targetPage, page_size: PAGE_SIZE });
        if (requestId !== requestIdRef.current) return; // superseded — drop

        // Resolve the (bounded) set of asset ids referenced on this page that
        // aren't already cached from a previous page.
        const uniqueAssetIds = Array.from(new Set(body.results.map((s) => s.asset)));
        const missingIds = uniqueAssetIds.filter((id) => !assetsById.has(id));
        let nextAssetsById = assetsById;
        if (missingIds.length > 0) {
          const fetched = await Promise.all(
            missingIds.map(async (id) => {
              try {
                return await api.getAsset(id);
              } catch {
                return null;
              }
            }),
          );
          if (requestId !== requestIdRef.current) return;
          nextAssetsById = new Map(assetsById);
          for (const asset of fetched) {
            if (asset) nextAssetsById.set(asset.id, asset);
          }
          setAssetsById(nextAssetsById);
        }

        setItems(body.results);
        setTotalCount(body.count);
        setPageState(targetPage);
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        setError(
          err instanceof ApiError
            ? err.problem.detail ?? err.problem.title
            : "Unable to reach the server. Please try again.",
        );
        setItems([]);
        setTotalCount(null);
      } finally {
        if (requestId === requestIdRef.current) setLoading(false);
      }
    },
    // `assetsById` deliberately omitted — read via closure for the
    // already-cached check, but re-including it would refetch on every asset
    // resolution. `filtersKey`/`filters` content is what should drive refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filtersKey],
  );

  useEffect(() => {
    void fetchPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey]);

  const setPage = useCallback(
    (targetPage: number) => {
      void fetchPage(targetPage);
    },
    [fetchPage],
  );

  const reload = useCallback(() => {
    void fetchPage(page);
  }, [fetchPage, page]);

  const pageCount = totalCount !== null ? Math.max(1, Math.ceil(totalCount / PAGE_SIZE)) : 1;

  return { items, assetsById, totalCount, page, pageCount, loading, error, setPage, reload };
}
