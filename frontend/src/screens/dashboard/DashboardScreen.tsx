import { Badge, Button, Card, Group, Loader, SimpleGrid, Stack, Text, Title } from "@mantine/core";
import { IconCategory, IconMapPin } from "@tabler/icons-react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import { DashboardTiles, DashboardTilesError, DashboardTilesSkeleton } from "./DashboardTiles";
import { useDashboardSummary } from "./useDashboardSummary";

/**
 * Dashboard / Home screen (T5.6, `docs/api-and-ui.md`: "Tiles: totals by
 * category, currently-out, overdue, low-stock, upcoming reservations,
 * per-project allocation"). Post-login landing route (`/`).
 *
 * Redesigned around the shared `AppLayout` nav shell: the old vertical stack
 * of full-width nav buttons is gone (that's what the sidebar/bottom-tab bar
 * now is) — this screen is just the tiles (the real content) plus a compact
 * profile/settings card for memberships + admin quick links.
 */
export function DashboardScreen() {
  const { me } = useAuth();
  const navigate = useNavigate();
  const { summary, loading, error, reload } = useDashboardSummary();

  if (!me) {
    return (
      <AppLayout title="Dashboard">
        <Group justify="center" p="xl">
          <Loader />
        </Group>
      </AppLayout>
    );
  }

  return (
    <AppLayout title="Dashboard">
      <Stack gap="lg" data-testid="home-shell">
        <Stack gap={0}>
          <Title order={3}>Welcome back, {me.name.split(" ")[0]}</Title>
          <Text c="dimmed" size="sm">
            Here&apos;s what&apos;s happening across {me.tenant.name} right now.
          </Text>
        </Stack>

{loading && !summary && <DashboardTilesSkeleton />}
        {error && !summary && <DashboardTilesError message={error} onRetry={reload} />}
        {summary && <DashboardTiles summary={summary} />}

        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
          <Card withBorder padding="lg">
            <Group justify="space-between" mb="sm">
              <Text fw={700}>Memberships</Text>
              <Badge variant="light" color="brand">
                {me.memberships.length}
              </Badge>
            </Group>
            {me.memberships.length === 0 ? (
              <Text c="dimmed" size="sm">
                No memberships.
              </Text>
            ) : (
              <Stack gap="xs">
                {me.memberships.map((m, idx) => (
                  <Group key={idx} justify="space-between">
                    <Text size="sm">
                      {m.project_name ? `Project: ${m.project_name}` : "Tenant-wide"}
                    </Text>
                    <Badge variant="outline">{m.role_name}</Badge>
                  </Group>
                ))}
              </Stack>
            )}
          </Card>

          <Card withBorder padding="lg">
            <Text fw={700} mb="sm">
              Admin
            </Text>
            <Stack gap="xs">
              <Button
                variant="light"
                justify="space-between"
                fullWidth
                leftSection={<IconCategory size={16} />}
                onClick={() => navigate("/admin/categories")}
              >
                Categories &amp; Fields
              </Button>
              <Button
                variant="light"
                justify="space-between"
                fullWidth
                leftSection={<IconMapPin size={16} />}
                onClick={() => navigate("/admin/locations")}
              >
                Locations
              </Button>
              <Text size="xs" c="dimmed" mt={4}>
                Visible to anyone who can view the inventory tree; write actions
                inside are gated by <code>category.manage</code>/
                <code>location.manage</code> (presentation only — the server is
                the real authority).
              </Text>
            </Stack>
          </Card>
        </SimpleGrid>
      </Stack>
    </AppLayout>
  );
}
