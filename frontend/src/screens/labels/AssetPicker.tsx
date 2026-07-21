import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  Card,
  Center,
  Checkbox,
  Group,
  Loader,
  ScrollArea,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { useAssetList } from "../assets/useAssetList";
import type { Asset, AssetListParams } from "../../api/types";

interface AssetPickerProps {
  selectedIds: Set<number>;
  onToggle: (asset: Asset) => void;
  onClearSelection: () => void;
}

/**
 * Minimal asset multi-select for the Labels screen (T4.5 task instructions:
 * "reuse the existing Asset List's selection mechanism if it has multi-select
 * already ... if it doesn't, add a minimal one scoped to this screen"). The
 * Asset List (`AssetListScreen`/`AssetRows`) has no multi-select at all —
 * every row navigates to the detail screen on tap — so this is a small,
 * separate picker scoped to this screen only, reusing `useAssetList` (server-
 * side search + pagination, CLAUDE.md: "never load all assets") rather than
 * the full list screen's filter bar/virtualization/card-vs-table UI, none of
 * which this picker needs.
 *
 * Server-side RBAC already narrows what `useAssetList` can ever return to the
 * caller's own tenant + `asset.view` scope (`apps.assets.api.AssetViewSet.
 * get_queryset`); `label.generate` is a DIFFERENT, separately (and more
 * narrowly) scoped permission the server re-checks on submit — so an asset
 * appearing here is never a guarantee it can actually be labeled. The parent
 * screen surfaces that server-side outcome (job succeeded with fewer assets
 * than requested, or a 400) rather than this component trying to predict it.
 */
export function AssetPicker({ selectedIds, onToggle, onClearSelection }: AssetPickerProps) {
  const [search, setSearch] = useState("");

  const filters = useMemo<AssetListParams>(
    () => ({ search: search || undefined, ordering: "-created_at" }),
    [search],
  );

  const { assets, totalCount, initialLoading, loadingMore, error, hasMore, loadMore } =
    useAssetList({ filters, pageSize: 20 });

  return (
    <Stack gap="xs">
      <Group justify="space-between" wrap="wrap">
        <TextInput
          placeholder="Search assets to label…"
          value={search}
          onChange={(e) => setSearch(e.currentTarget.value)}
          aria-label="Search assets"
          data-testid="label-asset-search"
          style={{ flex: 1, minWidth: 200 }}
        />
        <Badge variant="light" data-testid="label-selected-count">
          {selectedIds.size} selected
        </Badge>
        {selectedIds.size > 0 && (
          <Button variant="subtle" size="xs" onClick={onClearSelection}>
            Clear selection
          </Button>
        )}
      </Group>

      {error && (
        <Text c="red" size="sm">
          {error}
        </Text>
      )}

      {initialLoading && (
        <Center p="md">
          <Loader size="sm" />
        </Center>
      )}

      {!initialLoading && !error && assets.length === 0 && (
        <Text c="dimmed" size="sm">
          No assets match your search.
        </Text>
      )}

      {!initialLoading && assets.length > 0 && (
        <ScrollArea.Autosize mah={360} data-testid="label-asset-picker-list">
          <Stack gap={4}>
            {assets.map((asset) => (
              <Card key={asset.id} withBorder padding="xs">
                <Checkbox
                  label={
                    <Stack gap={0}>
                      <Text size="sm" fw={500}>
                        {asset.name}
                      </Text>
                      {asset.serial_number && (
                        <Text size="xs" c="dimmed">
                          SN {asset.serial_number}
                        </Text>
                      )}
                    </Stack>
                  }
                  checked={selectedIds.has(asset.id)}
                  onChange={() => onToggle(asset)}
                  data-testid={`label-asset-checkbox-${asset.id}`}
                />
              </Card>
            ))}
          </Stack>
        </ScrollArea.Autosize>
      )}

      {hasMore && (
        <Button variant="light" size="xs" onClick={loadMore} loading={loadingMore}>
          Load more
        </Button>
      )}

      {totalCount !== null && (
        <Text size="xs" c="dimmed">
          Showing {assets.length} of {totalCount}
        </Text>
      )}
    </Stack>
  );
}
