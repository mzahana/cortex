import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { Asset, Checkout } from "../../api/types";

const PAGE_SIZE = 25;

interface UseMyItemsListResult {
  items: Checkout[];
  /** `Asset.id -> Asset`, resolved for every checkout currently on the page
   * (`Checkout.asset` is a plain id, not nested — see `Checkout` type doc
   * comment). Bounded to the current page size, never a full asset-table
   * load. */
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
 * Server-side paginated "my open items" list (T3.5, `GET /api/v1/checkouts?
 * open=true`). One bounded page at a time (CLAUDE.md: never load "all
 * checkouts") — the server already scopes the list to the caller's own
 * checkouts UNION their `checkout.manage`/`checkout.override` project scope
 * (`apps.reservations.checkout.CheckoutViewSet.get_queryset`), so no
 * `?user=me` param is needed or sent. Ordered soonest-due-first so an
 * overdue/near-due item surfaces at the top of a one-handed phone screen.
 * Same "resolve missing assets for the current page" pattern as
 * `useStockList`/`useReservationList`.
 */
export function useMyItemsList(): UseMyItemsListResult {
  const [items, setItems] = useState<Checkout[]>([]);
  const [assetsById, setAssetsById] = useState<Map<number, Asset>>(new Map());
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [page, setPageState] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const requestIdRef = useRef(0);

  const fetchPage = useCallback(
    async (targetPage: number) => {
      const requestId = ++requestIdRef.current;
      setLoading(true);
      setError(null);
      try {
        const body = await api.listCheckouts({
          open: true,
          ordering: "due_at",
          page: targetPage,
          page_size: PAGE_SIZE,
        });
        if (requestId !== requestIdRef.current) return; // superseded — drop

        const uniqueAssetIds = Array.from(new Set(body.results.map((c) => c.asset)));
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
    // `assetsById` deliberately omitted — see `useStockList`'s identical note.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  useEffect(() => {
    void fetchPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
