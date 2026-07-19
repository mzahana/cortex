import { Group, SegmentedControl, Select, Stack, Switch, TextInput } from "@mantine/core";
import { ORDERING_OPTIONS, STATUS_OPTIONS, type AssetOrderingOrRelevance } from "./assetConstants";
import type { ViewMode } from "./types";

interface SelectOption {
  value: string;
  label: string;
}

export interface AssetFiltersValue {
  search: string;
  categoryId: string | null;
  locationId: string | null;
  projectId: string | null;
  tagId: string | null;
  status: string | null;
  isConsumable: string | null;
  includeRetired: boolean;
  ordering: AssetOrderingOrRelevance;
  viewMode: ViewMode;
}

interface AssetFiltersProps {
  value: AssetFiltersValue;
  onChange: (next: AssetFiltersValue) => void;
  categoryOptions: SelectOption[];
  locationOptions: SelectOption[];
  projectOptions: SelectOption[];
  tagOptions: SelectOption[];
}

const IS_CONSUMABLE_OPTIONS: SelectOption[] = [
  { value: "true", label: "Consumable" },
  { value: "false", label: "Durable" },
];

/**
 * The Asset List's filter bar (T1.6, docs/api-and-ui.md: "server-side search
 * + filters + tags"). Every control here maps 1:1 onto a documented
 * `AssetListParams` facet (`category,status,location,project,tag,
 * is_consumable`) — nothing here does client-side filtering; the parent
 * screen feeds these values straight into `api.listAssets`. Mobile-first:
 * stacks to full-width single-column controls on narrow viewports.
 */
export function AssetFilters({
  value,
  onChange,
  categoryOptions,
  locationOptions,
  projectOptions,
  tagOptions,
}: AssetFiltersProps) {
  const set = <K extends keyof AssetFiltersValue>(key: K, v: AssetFiltersValue[K]) =>
    onChange({ ...value, [key]: v });

  return (
    <Stack gap="xs">
      <TextInput
        placeholder="Search name, serial, spec…"
        value={value.search}
        onChange={(e) => set("search", e.currentTarget.value)}
        aria-label="Search assets"
        data-testid="asset-search"
      />

      <Group gap="xs" wrap="wrap">
        <Select
          placeholder="Category"
          data={categoryOptions}
          value={value.categoryId}
          onChange={(v) => set("categoryId", v)}
          clearable
          searchable
          w={180}
          aria-label="Filter by category"
        />
        <Select
          placeholder="Location"
          data={locationOptions}
          value={value.locationId}
          onChange={(v) => set("locationId", v)}
          clearable
          searchable
          w={180}
          aria-label="Filter by location"
        />
        <Select
          placeholder="Project"
          data={projectOptions}
          value={value.projectId}
          onChange={(v) => set("projectId", v)}
          clearable
          searchable
          w={160}
          aria-label="Filter by project"
        />
        <Select
          placeholder="Tag"
          data={tagOptions}
          value={value.tagId}
          onChange={(v) => set("tagId", v)}
          clearable
          searchable
          w={140}
          aria-label="Filter by tag"
        />
        <Select
          placeholder="Status"
          data={STATUS_OPTIONS}
          value={value.status}
          onChange={(v) => set("status", v)}
          clearable
          w={140}
          aria-label="Filter by status"
        />
        <Select
          placeholder="Consumable?"
          data={IS_CONSUMABLE_OPTIONS}
          value={value.isConsumable}
          onChange={(v) => set("isConsumable", v)}
          clearable
          w={140}
          aria-label="Filter by consumable/durable"
        />
        <Select
          placeholder="Sort"
          data={ORDERING_OPTIONS}
          value={value.ordering}
          onChange={(v) => v && set("ordering", v as AssetOrderingOrRelevance)}
          w={190}
          aria-label="Sort order"
          allowDeselect={false}
        />
      </Group>

      <Group justify="space-between" wrap="wrap">
        <Switch
          label="Include retired"
          checked={value.includeRetired}
          onChange={(e) => set("includeRetired", e.currentTarget.checked)}
        />
        <SegmentedControl
          value={value.viewMode}
          onChange={(v) => set("viewMode", v as ViewMode)}
          data={[
            { value: "card", label: "Cards" },
            { value: "table", label: "Table" },
          ]}
          aria-label="Card or table view"
        />
      </Group>
    </Stack>
  );
}
