import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { Asset, Reservation, ReservationListParams } from "../../api/types";

const PAGE_SIZE = 100;

interface UseReservationListOptions {
  /** Caller-owned filter object — `useMemo`'d by value so this hook only
   * refetches when the actual filter *content* changes (same pattern as
   * `useStockList`/`useReorderRequests`). */
  filters: ReservationListParams;
  /** Set `false` to skip fetching (e.g. while a date-range picker is mid-edit). */
  enabled?: boolean;
}

interface UseReservationListResult {
  items: Reservation[];
  /** `Asset.id -> Asset`, resolved for every reservation currently loaded
   * (`Reservation.asset` is a plain id, not nested — see `Reservation` type
   * doc comment). Bounded to the current page, never a full asset-table load. */
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
 * Server-side paginated reservation list (T3.4, `GET /api/v1/reservations`) —
 * one bounded page at a time (CLAUDE.md: never load "all reservations"). Used
 * by both the Calendar (`?from&to`) and the Approvals screen (`?status=pending`).
 * A generous `PAGE_SIZE` (100) covers a typical calendar window in one request;
 * `totalCount` vs. `items.length` tells the caller if a narrower window/page
 * would be needed to see the rest.
 */
export function useReservationList({
  filters,
  enabled = true,
}: UseReservationListOptions): UseReservationListResult {
  const [items, setItems] = useState<Reservation[]>([]);
  const [assetsById, setAssetsById] = useState<Map<number, Asset>>(new Map());
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [page, setPageState] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const requestIdRef = useRef(0);
  const filtersKey = JSON.stringify(filters);

  const fetchPage = useCallback(
    async (targetPage: number) => {
      if (!enabled) return;
      const requestId = ++requestIdRef.current;
      setLoading(true);
      setError(null);
      try {
        const body = await api.listReservations({
          ...filters,
          page: targetPage,
          page_size: PAGE_SIZE,
        });
        if (requestId !== requestIdRef.current) return;

        const uniqueAssetIds = Array.from(new Set(body.results.map((r) => r.asset)));
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
    [filtersKey, enabled],
  );

  useEffect(() => {
    void fetchPage(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey, enabled]);

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
