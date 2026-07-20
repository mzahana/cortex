import { Badge, Button, Card, Group, Stack, Text } from "@mantine/core";
import type { Asset, StockItem } from "../../api/types";

interface StockRowProps {
  stockItem: StockItem;
  asset: Asset | undefined;
  binLocationName: string;
  canAdjust: boolean;
  canConsume: boolean;
  canRequestReorder: boolean;
  onReceive: () => void;
  onConsume: () => void;
  onAdjust: () => void;
  onRequestReorder: () => void;
}

/** One `StockItem` row (T2.4). Low-stock (`quantity_on_hand <=
 * reorder_threshold`, the same comparison the server's `?low_stock=true`
 * filter and T2.3's partial index use) is visually highlighted with a red
 * "Low stock" badge and a tinted card so it's obvious at a glance, whether
 * or not the low-stock filter toggle is on. */
export function StockRow({
  stockItem,
  asset,
  binLocationName,
  canAdjust,
  canConsume,
  canRequestReorder,
  onReceive,
  onConsume,
  onAdjust,
  onRequestReorder,
}: StockRowProps) {
  const isLowStock = stockItem.quantity_on_hand <= stockItem.reorder_threshold;

  return (
    <Card
      withBorder
      padding="sm"
      data-testid={`stock-row-${stockItem.id}`}
      style={isLowStock ? { borderColor: "var(--mantine-color-red-5)" } : undefined}
    >
      <Stack gap={6}>
        <Group justify="space-between" wrap="nowrap">
          <Text fw={600} truncate>
            {asset?.name ?? `Asset #${stockItem.asset}`}
          </Text>
          {isLowStock && (
            <Badge color="red" data-testid={`low-stock-badge-${stockItem.id}`}>
              Low stock
            </Badge>
          )}
        </Group>

        <Group gap="lg" wrap="wrap">
          <Text size="sm">
            On hand: <strong>{stockItem.quantity_on_hand}</strong> {stockItem.unit_of_measure}
          </Text>
          <Text size="sm" c="dimmed">
            Reorder at {stockItem.reorder_threshold}, target {stockItem.reorder_target}
          </Text>
          {binLocationName && (
            <Text size="sm" c="dimmed">
              Bin: {binLocationName}
            </Text>
          )}
        </Group>

        <Group gap="xs" wrap="wrap">
          {canAdjust && (
            <Button size="xs" variant="light" onClick={onReceive} data-testid={`receive-${stockItem.id}`}>
              Receive
            </Button>
          )}
          {canConsume && (
            <Button size="xs" variant="light" onClick={onConsume} data-testid={`consume-${stockItem.id}`}>
              Consume
            </Button>
          )}
          {canAdjust && (
            <Button size="xs" variant="subtle" onClick={onAdjust} data-testid={`adjust-${stockItem.id}`}>
              Adjust
            </Button>
          )}
          {canRequestReorder && (
            <Button
              size="xs"
              variant={isLowStock ? "filled" : "outline"}
              color={isLowStock ? "red" : undefined}
              onClick={onRequestReorder}
              data-testid={`request-reorder-${stockItem.id}`}
            >
              Request reorder
            </Button>
          )}
        </Group>
      </Stack>
    </Card>
  );
}
