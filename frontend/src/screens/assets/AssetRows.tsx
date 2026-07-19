import type { CSSProperties } from "react";
import { Badge, Card, Group, Text, UnstyledButton } from "@mantine/core";
import type { Asset } from "../../api/types";
import { STATUS_COLORS, STATUS_LABELS } from "./assetConstants";

export interface AssetRowLookups {
  categoryName: string;
  locationName: string;
  projectName: string;
}

interface AssetRowProps extends AssetRowLookups {
  asset: Asset;
  onOpen: (id: number) => void;
  style?: CSSProperties;
}

/** Card view row (T1.6: "card and table views"). One asset per row, full
 * width — deliberately a single column, not a multi-column card grid, so
 * this stays comfortable one-handed on a phone (CLAUDE.md mobile-first). */
export function AssetCardRow({ asset, categoryName, locationName, projectName, onOpen, style }: AssetRowProps) {
  return (
    <div style={style} data-testid="asset-card-row">
      <UnstyledButton onClick={() => onOpen(asset.id)} style={{ width: "100%", display: "block" }}>
        <Card withBorder padding="sm" mb="xs" h="100%">
          <Group justify="space-between" wrap="nowrap" align="flex-start">
            <div style={{ minWidth: 0 }}>
              <Text fw={600} truncate>
                {asset.name}
              </Text>
              <Text size="xs" c="dimmed" truncate>
                {categoryName}
                {asset.serial_number ? ` · SN ${asset.serial_number}` : ""}
              </Text>
              <Text size="xs" c="dimmed" truncate>
                {locationName || "No location"}
                {projectName ? ` · ${projectName}` : " · General pool"}
              </Text>
            </div>
            <Badge color={STATUS_COLORS[asset.status]} variant="light" style={{ flexShrink: 0 }}>
              {STATUS_LABELS[asset.status]}
            </Badge>
          </Group>
          {asset.tags.length > 0 && (
            <Group gap={4} mt="xs" wrap="wrap">
              {asset.tags.map((tag) => (
                <Badge key={tag} size="xs" variant="dot" color="grape">
                  {tag}
                </Badge>
              ))}
            </Group>
          )}
        </Card>
      </UnstyledButton>
    </div>
  );
}

/** Table view row — plain CSS-grid columns (not a real `<table>`) so it can
 * share the same virtualized absolute-positioned-row layout as the card
 * view; a sticky header (`AssetTableHeader`) supplies the column labels. */
export function AssetTableRow({ asset, categoryName, locationName, projectName, onOpen, style }: AssetRowProps) {
  return (
    <div style={style} data-testid="asset-table-row">
      <UnstyledButton
        onClick={() => onOpen(asset.id)}
        style={{
          width: "100%",
          height: "100%",
          display: "grid",
          gridTemplateColumns: "2fr 1.2fr 0.9fr 1.2fr 1fr 1.4fr",
          alignItems: "center",
          gap: 8,
          padding: "6px 8px",
          borderBottom: "1px solid var(--mantine-color-default-border)",
        }}
      >
        <Text size="sm" fw={500} truncate>
          {asset.name}
        </Text>
        <Text size="sm" c="dimmed" truncate>
          {categoryName}
        </Text>
        <Badge color={STATUS_COLORS[asset.status]} variant="light" size="sm">
          {STATUS_LABELS[asset.status]}
        </Badge>
        <Text size="sm" c="dimmed" truncate>
          {locationName || "—"}
        </Text>
        <Text size="sm" c="dimmed" truncate>
          {projectName || "General pool"}
        </Text>
        <Group gap={4} wrap="nowrap" style={{ overflow: "hidden" }}>
          {asset.tags.slice(0, 3).map((tag) => (
            <Badge key={tag} size="xs" variant="dot" color="grape">
              {tag}
            </Badge>
          ))}
        </Group>
      </UnstyledButton>
    </div>
  );
}

export function AssetTableHeader() {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "2fr 1.2fr 0.9fr 1.2fr 1fr 1.4fr",
        gap: 8,
        padding: "6px 8px",
        fontWeight: 600,
        fontSize: "var(--mantine-font-size-xs)",
        borderBottom: "2px solid var(--mantine-color-default-border)",
        position: "sticky",
        top: 0,
        background: "var(--mantine-color-body)",
        zIndex: 1,
      }}
    >
      <Text size="xs" fw={700}>
        Name
      </Text>
      <Text size="xs" fw={700}>
        Category
      </Text>
      <Text size="xs" fw={700}>
        Status
      </Text>
      <Text size="xs" fw={700}>
        Location
      </Text>
      <Text size="xs" fw={700}>
        Project
      </Text>
      <Text size="xs" fw={700}>
        Tags
      </Text>
    </div>
  );
}
