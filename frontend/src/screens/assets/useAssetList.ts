import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { Asset, AssetListParams } from "../../api/types";

interface UseAssetListOptions {
  /** Caller-owned filter object — should be `useMemo`'d by value so this
   * hook only refetches when the actual filter *content* changes, not on
   * every render. */
  filters: AssetListParams;
  pageSize?: number;
}

interface UseAssetListResult {
  /** Every asset fetched so far across all pages loaded in this session —
   * NOT "all assets" (CLAUDE.md): this only ever grows one server page at a
   * time, via `loadMore()`, and the list screen renders only the
   * virtualized visible slice of it. */
  assets: Asset[];
  /** Server-reported total row count (`Paginated.count`) — lets the UI show
   * "37 of 10,512" and prove the list is server-paginated, not fully loaded. */
  totalCount: number | null;
  initialLoading: boolean;
  loadingMore: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => void;
  reload: () => void;
}

/**
 * Server-side pagination + infinite-scroll accumulation for the Asset List
 * (T1.6). Every fetch goes through `api.listAssets` — one bounded
 * `?page`/`?page_size` request at a time (CLAUDE.md: "never load all
 * assets"). A filter/search/ordering change resets to page 1 and replaces
 * the whole array; `loadMore()` appends the next page.
 */
export function useAssetList({ filters, pageSize = 50 }: UseAssetListOptions): UseAssetListResult {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);

  const pageRef = useRef(1);
  // Bumped on every fetch; a response whose id no longer matches the latest
  // is stale (a newer filter change or reload superseded it) and is
  // dropped — guards against a slow page-2 response landing after the user
  // has already changed filters and gotten a fresh page-1 back.
  const requestIdRef = useRef(0);

  // Filters are only ever compared/depended-on by their serialized content
  // (the caller constructs a fresh object each render) — this is the one
  // stable primitive to key effects/callbacks off of.
  const filtersKey = JSON.stringify(filters);

  const fetchPage = useCallback(
    async (page: number, replace: boolean) => {
      const requestId = ++requestIdRef.current;
      if (replace) {
        setInitialLoading(true);
        setError(null);
      } else {
        setLoadingMore(true);
      }
      try {
        const body = await api.listAssets({ ...filters, page, page_size: pageSize });
        if (requestId !== requestIdRef.current) return; // superseded — drop
        setAssets((prev) => (replace ? body.results : [...prev, ...body.results]));
        setTotalCount(body.count);
        setHasMore(body.next !== null);
        pageRef.current = page;
      } catch (err) {
        if (requestId !== requestIdRef.current) return;
        setError(
          err instanceof ApiError
            ? err.problem.detail ?? err.problem.title
            : "Unable to reach the server. Please try again.",
        );
        if (replace) {
          setAssets([]);
          setTotalCount(null);
          setHasMore(false);
        }
      } finally {
        if (requestId === requestIdRef.current) {
          setInitialLoading(false);
          setLoadingMore(false);
        }
      }
    },
    // `filters` itself is deliberately omitted: its *content* is fully
    // captured by `filtersKey` (a stable serialization) already in the dep
    // array, and re-including the object reference would refetch on every
    // render (a new caller-constructed object each time) rather than only
    // when the content actually changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filtersKey, pageSize],
  );

  useEffect(() => {
    pageRef.current = 1;
    void fetchPage(1, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey, pageSize]);

  const loadMore = useCallback(() => {
    if (loadingMore || initialLoading || !hasMore) return;
    void fetchPage(pageRef.current + 1, false);
  }, [fetchPage, loadingMore, initialLoading, hasMore]);

  const reload = useCallback(() => {
    pageRef.current = 1;
    void fetchPage(1, true);
  }, [fetchPage]);

  return { assets, totalCount, initialLoading, loadingMore, error, hasMore, loadMore, reload };
}
