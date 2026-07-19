import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  ActionIcon,
  Alert,
  AppShell,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { useNavigate } from "react-router-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
import { api, ApiError } from "../../api/client";
import { ASSET_CREATE, hasAnyAssetPermission } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import type { AssetListParams, Category, Location, Project, Tag } from "../../api/types";
import { buildTree, flattenForSelect } from "../../components/treeUtils";
import { AssetFilters, type AssetFiltersValue } from "./AssetFilters";
import { AssetCardRow, AssetTableHeader, AssetTableRow } from "./AssetRows";
import { useAssetList } from "./useAssetList";

const PAGE_SIZE = 50;
const CARD_ROW_HEIGHT = 118;
const TABLE_ROW_HEIGHT = 52;

const DEFAULT_FILTERS: AssetFiltersValue = {
  search: "",
  categoryId: null,
  locationId: null,
  projectId: null,
  tagId: null,
  status: null,
  isConsumable: null,
  includeRetired: false,
  // "Relevance" is the default precisely because it means "no explicit sort
  // chosen yet" — see `assetConstants.ts`'s `AssetOrderingOrRelevance` doc
  // comment (T1.6 review carried fix): this is what lets a search's FTS/
  // trigram `-rank` ordering actually take effect server-side instead of
  // always being pre-empted by a client-sent `?ordering=-created_at`.
  ordering: "relevance",
  viewMode: "card",
};

/**
 * Asset List (T1.6, docs/api-and-ui.md "Asset List": "Server-side search +
 * filters + tags; virtualized list; card + table views").
 *
 * Every facet (search/category/location/project/tag/status/is_consumable/
 * ordering) is sent straight to `GET /api/v1/assets` via `api.listAssets` —
 * there is no client-side filtering over an already-loaded array anywhere in
 * this screen (CLAUDE.md: "never load all assets"). Rows are rendered via
 * `@tanstack/react-virtual` so, however many pages have been fetched into
 * memory by scrolling, only the ~15-20 rows actually visible in the
 * viewport are ever mounted in the DOM.
 */
export function AssetListScreen() {
  const navigate = useNavigate();
  const { me } = useAuth();
  const canCreate = hasAnyAssetPermission(me, ASSET_CREATE);

  // --- Bounded catalog lookups for filter dropdowns + name rendering ---
  // (categories/locations/projects/tags are tenant config, not asset rows —
  // see `listAllCategories`/etc. doc comments in `api/client.ts`.)
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
          api.listAllProjects({ ordering: "name" }),
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
  }, []);

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
  const [filterState, setFilterState] = useState<AssetFiltersValue>(DEFAULT_FILTERS);
  const [debouncedSearch] = useDebouncedValue(filterState.search, 350);

  const filters = useMemo<AssetListParams>(
    () => ({
      search: debouncedSearch || undefined,
      // "relevance" (the default) is never sent — see `DEFAULT_FILTERS.ordering`
      // doc comment above; an explicit user-chosen sort always wins, matching
      // the server's own "explicit ?ordering= always wins" rule.
      ordering: filterState.ordering === "relevance" ? undefined : filterState.ordering,
      category: filterState.categoryId ? Number(filterState.categoryId) : undefined,
      location: filterState.locationId ? Number(filterState.locationId) : undefined,
      project: filterState.projectId ? Number(filterState.projectId) : undefined,
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
    ],
  );

  const { assets, totalCount, initialLoading, loadingMore, error, hasMore, loadMore, reload } = useAssetList({
    filters,
    pageSize: PAGE_SIZE,
  });

  const viewMode = filterState.viewMode;
  const rowHeight = viewMode === "card" ? CARD_ROW_HEIGHT : TABLE_ROW_HEIGHT;

  // --- Virtualized scroll container ---
  const parentRef = useRef<HTMLDivElement>(null);
  const rowVirtualizer = useVirtualizer({
    count: assets.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => rowHeight,
    overscan: 6,
  });

  const virtualItems = rowVirtualizer.getVirtualItems();
  const lastVirtualIndex = virtualItems.length ? virtualItems[virtualItems.length - 1].index : -1;

  // Infinite scroll: once the last rendered virtual row is within 10 rows of
  // the end of what's currently loaded, fetch the next server page. This is
  // the only place another page is ever requested — never on mount beyond
  // page 1, never "load everything".
  useEffect(() => {
    if (lastVirtualIndex === -1) return;
    if (lastVirtualIndex >= assets.length - 10 && hasMore && !loadingMore && !initialLoading) {
      loadMore();
    }
  }, [lastVirtualIndex, assets.length, hasMore, loadingMore, initialLoading, loadMore]);

  const resolveNames = (assetCategoryId: number, assetLocationId: number | null, assetProjectId: number | null) => ({
    categoryName: categoryNameById.get(assetCategoryId) ?? `Category #${assetCategoryId}`,
    locationName: assetLocationId ? locationNameById.get(assetLocationId) ?? `Location #${assetLocationId}` : "",
    projectName: assetProjectId ? projectNameById.get(assetProjectId) ?? `Project #${assetProjectId}` : "",
  });

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/")}>
              &#8592;
            </ActionIcon>
            <Title order={4}>Assets</Title>
          </Group>
          <Group gap="sm">
            <Text size="sm" c="dimmed" data-testid="asset-count">
              {totalCount !== null ? `${assets.length} of ${totalCount.toLocaleString()}` : ""}
            </Text>
            {canCreate && (
              <Button size="xs" onClick={() => navigate("/assets/new")} data-testid="new-asset-button">
                New asset
              </Button>
            )}
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Stack gap="sm" h="calc(100vh - 100px)">
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
      </AppShell.Main>
    </AppShell>
  );
}
