import type { ReactNode } from "react";
import { Alert, Badge, Button, Card, Group, SimpleGrid, Skeleton, Stack, Text, ThemeIcon } from "@mantine/core";
import type { Icon } from "@tabler/icons-react";
import {
  IconAlertTriangle,
  IconBoxSeam,
  IconCalendarStats,
  IconClockExclamation,
  IconPackageExport,
} from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";
import type { DashboardSummary } from "../../api/types";

function Tile({
  label,
  value,
  color,
  icon: TileIcon,
  onClick,
  testId,
  children,
}: {
  label: string;
  value: number | string;
  color?: string;
  icon: Icon;
  onClick?: () => void;
  testId: string;
  children?: ReactNode;
}) {
  return (
    <Card
      withBorder
      padding="md"
      radius="lg"
      onClick={onClick}
      style={onClick ? { cursor: "pointer" } : undefined}
      data-testid={testId}
    >
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <Stack gap={2}>
          <Text size="xs" c="dimmed" fw={600} tt="uppercase" style={{ letterSpacing: 0.3 }}>
            {label}
          </Text>
          <Text fw={800} size="1.75rem" c={color} lh={1.15}>
            {value}
          </Text>
          {children}
        </Stack>
        <ThemeIcon variant="light" color={color ?? "brand"} size={38} radius="md">
          <TileIcon size={20} stroke={1.75} />
        </ThemeIcon>
      </Group>
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
      <SimpleGrid cols={{ base: 2, sm: 3, lg: 5 }} spacing="sm">
        <Tile
          label="Currently out"
          value={summary.currently_out}
          icon={IconPackageExport}
          testId="tile-currently-out"
          onClick={() => navigate("/my-items")}
        />
        <Tile
          label="Overdue"
          value={summary.overdue}
          color={summary.overdue > 0 ? "red" : undefined}
          icon={IconClockExclamation}
          testId="tile-overdue"
          onClick={() => navigate("/my-items")}
        />
        <Tile
          label="Low stock"
          value={summary.low_stock}
          color={summary.low_stock > 0 ? "orange" : undefined}
          icon={IconAlertTriangle}
          testId="tile-low-stock"
          onClick={() => navigate("/stock")}
        />
        <Tile
          label={`Upcoming (${summary.upcoming_reservations_window_days}d)`}
          value={summary.upcoming_reservations}
          icon={IconCalendarStats}
          testId="tile-upcoming-reservations"
          onClick={() => navigate("/reservations")}
        />
        <Tile
          label="Total assets"
          value={summary.totals_by_category.reduce((sum, row) => sum + row.count, 0)}
          icon={IconBoxSeam}
          testId="tile-total-assets"
          onClick={() => navigate("/assets")}
        />
      </SimpleGrid>

      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
        <Card withBorder padding="lg" radius="lg" data-testid="tile-totals-by-category">
          <Stack gap="sm">
            <Text fw={700}>Totals by category</Text>
            {summary.totals_by_category.length === 0 && (
              <Text size="sm" c="dimmed">
                No assets yet.
              </Text>
            )}
            <Group gap="xs" wrap="wrap">
              {summary.totals_by_category.map((row) => (
                <Badge key={row.category_id ?? "none"} variant="light" color="brand" size="lg">
                  {row.category_name ?? "Uncategorized"}: {row.count}
                </Badge>
              ))}
            </Group>
          </Stack>
        </Card>

        <Card withBorder padding="lg" radius="lg" data-testid="tile-per-project-allocation">
          <Stack gap="sm">
            <Text fw={700}>Per-project allocation</Text>
            {summary.per_project_allocation.length === 0 && (
              <Text size="sm" c="dimmed">
                No project allocations yet.
              </Text>
            )}
            <Stack gap={6}>
              {summary.per_project_allocation.map((row) => (
                <Group key={row.project_id ?? "general"} justify="space-between">
                  <Text size="sm">{row.project_name}</Text>
                  <Badge variant="outline">{row.count}</Badge>
                </Group>
              ))}
            </Stack>
          </Stack>
        </Card>
      </SimpleGrid>
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
