import type { ReactNode } from "react";
import { Alert, Badge, Button, Card, Group, SimpleGrid, Skeleton, Stack, Text } from "@mantine/core";
import { useNavigate } from "react-router-dom";
import type { DashboardSummary } from "../../api/types";

function Tile({
  label,
  value,
  color,
  onClick,
  testId,
  children,
}: {
  label: string;
  value: number | string;
  color?: string;
  onClick?: () => void;
  testId: string;
  children?: ReactNode;
}) {
  return (
    <Card
      withBorder
      padding="md"
      radius="md"
      onClick={onClick}
      style={onClick ? { cursor: "pointer" } : undefined}
      data-testid={testId}
    >
      <Stack gap={4}>
        <Text size="sm" c="dimmed">
          {label}
        </Text>
        <Text fw={700} size="xl" c={color}>
          {value}
        </Text>
        {children}
      </Stack>
    </Card>
  );
}

/**
 * The six Dashboard/Home tiles (T5.6, `docs/api-and-ui.md`: "Tiles: totals by
 * category, currently-out, overdue, low-stock, upcoming reservations,
 * per-project allocation"). Pure presentation over `GET /dashboard/summary`
 * — every number is already scoped server-side to the caller's viewable
 * projects, rendered as-is. Loading/error/empty handled by the caller
 * (`DashboardScreen`); this component only renders once `summary` is present.
 */
export function DashboardTiles({ summary }: { summary: DashboardSummary }) {
  const navigate = useNavigate();

  return (
    <Stack gap="md" data-testid="dashboard-tiles">
      <SimpleGrid cols={{ base: 2, sm: 3 }} spacing="sm">
        <Tile
          label="Currently out"
          value={summary.currently_out}
          testId="tile-currently-out"
          onClick={() => navigate("/my-items")}
        />
        <Tile
          label="Overdue"
          value={summary.overdue}
          color={summary.overdue > 0 ? "red" : undefined}
          testId="tile-overdue"
          onClick={() => navigate("/my-items")}
        />
        <Tile
          label="Low stock"
          value={summary.low_stock}
          color={summary.low_stock > 0 ? "orange" : undefined}
          testId="tile-low-stock"
          onClick={() => navigate("/stock")}
        />
        <Tile
          label={`Upcoming reservations (${summary.upcoming_reservations_window_days}d)`}
          value={summary.upcoming_reservations}
          testId="tile-upcoming-reservations"
          onClick={() => navigate("/reservations")}
        />
        <Tile
          label="Total assets"
          value={summary.totals_by_category.reduce((sum, row) => sum + row.count, 0)}
          testId="tile-total-assets"
          onClick={() => navigate("/assets")}
        />
      </SimpleGrid>

      <Card withBorder padding="md" radius="md" data-testid="tile-totals-by-category">
        <Stack gap="xs">
          <Text fw={600}>Totals by category</Text>
          {summary.totals_by_category.length === 0 && (
            <Text size="sm" c="dimmed">
              No assets yet.
            </Text>
          )}
          <Group gap="xs" wrap="wrap">
            {summary.totals_by_category.map((row) => (
              <Badge key={row.category_id ?? "none"} variant="light" size="lg">
                {row.category_name ?? "Uncategorized"}: {row.count}
              </Badge>
            ))}
          </Group>
        </Stack>
      </Card>

      <Card withBorder padding="md" radius="md" data-testid="tile-per-project-allocation">
        <Stack gap="xs">
          <Text fw={600}>Per-project allocation</Text>
          {summary.per_project_allocation.length === 0 && (
            <Text size="sm" c="dimmed">
              No project allocations yet.
            </Text>
          )}
          <Stack gap={4}>
            {summary.per_project_allocation.map((row) => (
              <Group key={row.project_id ?? "general"} justify="space-between">
                <Text size="sm">{row.project_name}</Text>
                <Badge variant="outline">{row.count}</Badge>
              </Group>
            ))}
          </Stack>
        </Stack>
      </Card>
    </Stack>
  );
}

export function DashboardTilesSkeleton() {
  return (
    <SimpleGrid cols={{ base: 2, sm: 3 }} spacing="sm" data-testid="dashboard-tiles-loading">
      {Array.from({ length: 5 }).map((_, idx) => (
        <Skeleton key={idx} height={80} radius="md" />
      ))}
    </SimpleGrid>
  );
}

export function DashboardTilesError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <Alert color="red" title="Couldn't load the dashboard">
      <Stack gap="xs" align="flex-start">
        <Text size="sm">{message}</Text>
        <Button size="xs" variant="light" onClick={onRetry}>
          Retry
        </Button>
      </Stack>
    </Alert>
  );
}
