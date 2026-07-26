import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Alert, Box, Button, Center, Group, Loader, Stack, Text } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { useNavigate } from "react-router-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api, ApiError } from "../../api/client";
import {
  ASSET_CREATE,
  hasAnyAssetPermission,
  hasAssetExportPermission,
  hasAssetPermission,
} from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import type { Asset, AssetListParams, Category, Location, Paginated, Project, Tag } from "../../api/types";
import { buildTree, flattenForSelect } from "../../components/treeUtils";
import { AssetFilters, type AssetFiltersValue } from "./AssetFilters";
import { AssetCardRow, AssetTableHeader, AssetTableRow } from "./AssetRows";
import { useAssetList } from "./useAssetList";

const PAGE_SIZE = 50;
const CARD_ROW_HEIGHT = 118;
const TABLE_ROW_HEIGHT = 52;

function defaultFilters(): AssetFiltersValue {
  return {
    search: "",
    categoryId: null,
    locationId: null,
    projectId: null,
    tagId: null,
    status: null,
    isConsumable: null,
    includeRetired: false,
    ordering: "relevance",
    viewMode: "card",
    // suppressed entirely when `showProjectFilter` is false (project-scoped
    // embedding, see `ProjectAssetsTab`) — nothing here ever sends
    // `?project=` in that mode, the server-side endpoint is already fixed
    // to one project.
  };
}

export interface AssetListViewProps {
  /** Server-side fetch for one page — `api.listAssets` for the full Asset
   * List, or `(params) => api.listProjectAssets(projectId, params)` for the
   * project hub's Assets tab (`docs/tasks/M7-project-grants.md`: "the
   * project hub's Assets tab MUST reuse the existing asset list UI/
   * component... not a reimplementation"). Same `Paginated<Asset>` envelope,
   * same `AssetListParams` shape either way — this component has no idea
   * which endpoint it's actually hitting. */
  fetchAssets: (params: AssetListParams) => Promise<Paginated<Asset>>;
  /** `false` when embedded in a single project's context (the project hub's
   * Assets tab) — the project is already fixed by `fetchAssets`, so showing
   * a redundant "Project" filter dropdown (and sending a `?project=` the
   * server ignores) would be confusing. Also suppresses the project catalog
   * lookup fetch/option list and the row's own project name column since
   * it's always the same value in that context. */
  showProjectFilter?: boolean;
  /** Only meaningful when `showProjectFilter` is `false`: the fixed project
   * this list is scoped to, used to (a) scope the "New asset"/export CSV
   * gating checks (`hasAssetPermission`, project-scoped) and (b) preset the
   * new-asset form's project and the CSV export's `?project=`. */
  fixedProjectId?: number;
  /** CSS height for the scroll container — the full-screen Asset List uses
   * `calc(100vh - 140px)` (its own filters bar sits directly under the
   * shared `AppLayout` header); an embedded tab has less predictable
   * chrome above it, so callers there should pass something shorter. */
  containerHeight?: string;
}

/**
 * The reusable core of the Asset List (T1.6, docs/api-and-ui.md "Asset
 * List": "Server-side search + filters + tags; virtualized list; card +
 * table views") — extracted from `AssetListScreen` (M7,
 * `docs/tasks/M7-project-grants.md`) so the project hub's Assets tab can
 * embed the exact same filter bar / virtualized card-or-table rendering
 * against a DIFFERENT server-side fetch (`GET /projects/{id}/assets`
 * instead of `GET /assets`) without a parallel reimplementation.
 * `AssetListScreen` itself is now a thin `AppLayout` wrapper around this.
 *
 * Every facet is still sent straight to the server via `fetchAssets` — no
 * client-side filtering over an already-loaded array anywhere in this
 * component (CLAUDE.md: "never load all assets"). Rows are rendered via
 * `@tanstack/react-virtual` regardless of which endpoint feeds them.
 */
export function AssetListView({
  fetchAssets,
  showProjectFilter = true,
  fixedProjectId,
  containerHeight = "calc(100vh - 140px)",
}: AssetListViewProps) {
  const navigate = useNavigate();
  const { me } = useAuth();
  // Full Asset List (no fixed project): "holds it anywhere" gate, same as
  // the original `AssetListScreen`. Embedded project tab: scoped exactly to
  // THIS project, mirroring the server's own object-level check
  // (`apps.projects.permissions.ProjectPermission`).
  const canCreate =
    fixedProjectId !== undefined
      ? hasAssetPermission(me, ASSET_CREATE, fixedProjectId)
      : hasAnyAssetPermission(me, ASSET_CREATE);
  const canExport = hasAssetExportPermission(me);

  // --- Bounded catalog lookups for filter dropdowns + name rendering ---
  const [categories, setCategories] = useState<Category[]>([]);
  const [locations, setLocations] = useState<Location[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [tags, setTags] = useState<Tag[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [cats, locs, projs, tgs] = await Promise.all([
          api.listAllCategories({ ordering: "name" }),
          api.listAllLocations({ ordering: "name" }),
          showProjectFilter ? api.listAllProjects({ ordering: "name" }) : Promise.resolve([]),
          api.listAllTags({ ordering: "name" }),
        ]);
        if (cancelled) return;
        setCategories(cats);
        setLocations(locs);
        setProjects(projs);
        setTags(tgs);
      } catch (err) {
        if (cancelled) return;
        setCatalogError(
          err instanceof ApiError
            ? `Filters couldn't fully load: ${err.problem.detail ?? err.problem.title}`
            : "Filters couldn't fully load (backend unreachable).",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showProjectFilter]);

  const categoryOptions = useMemo(() => flattenForSelect(buildTree(categories)), [categories]);
  const locationOptions = useMemo(() => flattenForSelect(buildTree(locations)), [locations]);
  const projectOptions = useMemo(
    () => projects.map((p) => ({ value: String(p.id), label: p.name })),
    [projects],
  );
  const tagOptions = useMemo(() => tags.map((t) => ({ value: String(t.id), label: t.name })), [tags]);

  const categoryNameById = useMemo(() => new Map(categories.map((c) => [c.id, c.name])), [categories]);
  const locationNameById = useMemo(() => new Map(locations.map((l) => [l.id, l.name])), [locations]);
  const projectNameById = useMemo(() => new Map(projects.map((p) => [p.id, p.name])), [projects]);

  // --- Filter state ---
  const [filterState, setFilterState] = useState<AssetFiltersValue>(defaultFilters);
  const [debouncedSearch] = useDebouncedValue(filterState.search, 350);

  const filters = useMemo<AssetListParams>(
    () => ({
      search: debouncedSearch || undefined,
      ordering: filterState.ordering === "relevance" ? undefined : filterState.ordering,
      category: filterState.categoryId ? Number(filterState.categoryId) : undefined,
      location: filterState.locationId ? Number(filterState.locationId) : undefined,
      project: showProjectFilter && filterState.projectId ? Number(filterState.projectId) : undefined,
      tag: filterState.tagId ? Number(filterState.tagId) : undefined,
      status: (filterState.status as AssetListParams["status"]) || undefined,
      is_consumable:
        filterState.isConsumable === null || filterState.isConsumable === ""
          ? undefined
          : filterState.isConsumable === "true",
      include_retired: filterState.includeRetired || undefined,
    }),
    [
      debouncedSearch,
      filterState.ordering,
      filterState.categoryId,
      filterState.locationId,
      filterState.projectId,
      filterState.tagId,
      filterState.status,
      filterState.isConsumable,
      filterState.includeRetired,
      showProjectFilter,
    ],
  );

  const { assets, totalCount, initialLoading, loadingMore, error, hasMore, loadMore, reload } =
    useAssetList({ filters, pageSize: PAGE_SIZE, fetcher: fetchAssets });

  const viewMode = filterState.viewMode;
  const rowHeight = viewMode === "card" ? CARD_ROW_HEIGHT : TABLE_ROW_HEIGHT;

  const parentRef = useRef<HTMLDivElement>(null);
  const rowVirtualizer = useVirtualizer({
    count: assets.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => rowHeight,
    overscan: 6,
  });

  const virtualItems = rowVirtualizer.getVirtualItems();
  const lastVirtualIndex = virtualItems.length ? virtualItems[virtualItems.length - 1].index : -1;

  useEffect(() => {
    if (lastVirtualIndex === -1) return;
    if (lastVirtualIndex >= assets.length - 10 && hasMore && !loadingMore && !initialLoading) {
      loadMore();
    }
  }, [lastVirtualIndex, assets.length, hasMore, loadingMore, initialLoading, loadMore]);

  const resolveNames = (assetCategoryId: number, assetLocationId: number | null, assetProjectId: number | null) => ({
    categoryName: categoryNameById.get(assetCategoryId) ?? `Category #${assetCategoryId}`,
    locationName: assetLocationId ? locationNameById.get(assetLocationId) ?? `Location #${assetLocationId}` : "",
    projectName: showProjectFilter
      ? assetProjectId
        ? projectNameById.get(assetProjectId) ?? `Project #${assetProjectId}`
        : ""
      : "",
  });

  const newAssetHref =
    fixedProjectId !== undefined ? `/assets/new?project=${fixedProjectId}` : "/assets/new";
  const exportParams: AssetListParams =
    fixedProjectId !== undefined ? { ...filters, project: fixedProjectId } : filters;

  return (
    <Stack gap="sm" h={containerHeight}>
      <Group justify="flex-end" gap="xs">
        <Text size="sm" c="dimmed" data-testid="asset-count" style={{ marginRight: "auto" }}>
          {totalCount !== null ? `${assets.length} of ${totalCount.toLocaleString()}` : ""}
        </Text>
        {canExport && (
          <Button
            component="a"
            href={api.exportAssetsCsvUrl(exportParams)}
            variant="light"
            size="xs"
            data-testid="export-csv-button"
          >
            Export CSV
          </Button>
        )}
        {canCreate && (
          <Button size="xs" onClick={() => navigate(newAssetHref)} data-testid="new-asset-button">
            New asset
          </Button>
        )}
      </Group>

      {catalogError && (
        <Alert color="yellow" data-testid="catalog-filters-error">
          {catalogError}
        </Alert>
      )}

      <AssetFilters
        value={filterState}
        onChange={setFilterState}
        categoryOptions={categoryOptions}
        locationOptions={locationOptions}
        projectOptions={projectOptions}
        tagOptions={tagOptions}
        showProjectFilter={showProjectFilter}
      />

      {error && (
        <Alert color="red" data-testid="asset-list-error" title="Couldn't load assets">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{error}</Text>
            <Button size="xs" variant="light" onClick={reload}>
              Retry
            </Button>
          </Stack>
        </Alert>
      )}

      {initialLoading && !error && (
        <Center p="xl">
          <Loader data-testid="asset-list-loading" />
        </Center>
      )}

      {!initialLoading && !error && assets.length === 0 && (
        <Center p="xl">
          <Stack align="center" gap={4}>
            <Text fw={600}>No assets match these filters.</Text>
            <Text size="sm" c="dimmed">
              Try clearing a filter or broadening your search.
            </Text>
          </Stack>
        </Center>
      )}

      {!initialLoading && !error && assets.length > 0 && (
        <Box style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
          {viewMode === "table" && <AssetTableHeader />}
          <div
            ref={parentRef}
            data-testid="asset-list-scroll"
            style={{ flex: 1, overflow: "auto", position: "relative" }}
          >
            <div
              style={{
                height: rowVirtualizer.getTotalSize(),
                width: "100%",
                position: "relative",
              }}
            >
              {virtualItems.map((virtualRow) => {
                const asset = assets[virtualRow.index];
                const { categoryName, locationName, projectName } = resolveNames(
                  asset.category,
                  asset.location,
                  asset.project,
                );
                const style: CSSProperties = {
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  height: virtualRow.size,
                  transform: `translateY(${virtualRow.start}px)`,
                };
                const RowComponent = viewMode === "card" ? AssetCardRow : AssetTableRow;
                return (
                  <RowComponent
                    key={asset.id}
                    asset={asset}
                    categoryName={categoryName}
                    locationName={locationName}
                    projectName={projectName}
                    onOpen={(id) => navigate(`/assets/${id}`)}
                    style={style}
                  />
                );
              })}
            </div>
            {loadingMore && (
              <Center p="sm" data-testid="asset-list-loading-more">
                <Loader size="sm" />
              </Center>
            )}
          </div>
        </Box>
      )}
    </Stack>
  );
}
