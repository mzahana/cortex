import { useState } from "react";
import {
  ActionIcon,
  AppShell,
  Badge,
  Button,
  Group,
  Loader,
  Stack,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../hooks/useAuth";
import { hasAuditViewPermission } from "../../api/permissions";
import { DashboardTiles, DashboardTilesError, DashboardTilesSkeleton } from "./DashboardTiles";
import { useDashboardSummary } from "./useDashboardSummary";

/**
 * Dashboard / Home screen (T5.6, `docs/api-and-ui.md`: "Tiles: totals by
 * category, currently-out, overdue, low-stock, upcoming reservations,
 * per-project allocation"). This is the post-login landing route (`/`),
 * replacing the T0.7 placeholder shell — it keeps that shell's chrome
 * (header, logout, quick-nav buttons, membership summary) and drops the
 * raw permission-key badge dump (developer scaffolding, not a documented
 * user feature), adding the six live tiles above them. Mobile-first: tiles
 * are the first
 * thing seen, nav buttons are large and thumb-reachable below.
 */
export function DashboardScreen() {
  const { me, logout } = useAuth();
  const navigate = useNavigate();
  const [loggingOut, setLoggingOut] = useState(false);
  const { summary, loading, error, reload } = useDashboardSummary();

  if (!me) {
    return (
      <Group justify="center" p="xl">
        <Loader />
      </Group>
    );
  }

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await logout();
    } catch {
      // `logout()` itself is best-effort and swallows its own errors (see
      // `useAuth.logout`); this catch is defense-in-depth so a failure
      // anywhere else in this handler can never leave the button stuck in
      // its loading state or throw an unhandled rejection.
    } finally {
      setLoggingOut(false);
      navigate("/login", { replace: true });
    }
  };

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Title order={4}>Cortex</Title>
          <Tooltip label="Log out">
            <ActionIcon
              variant="subtle"
              aria-label="Log out"
              onClick={handleLogout}
              loading={loggingOut}
              size="lg"
            >
              &#x23FB;
            </ActionIcon>
          </Tooltip>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Stack gap="lg" data-testid="home-shell">
          <Stack gap={0}>
            <Text size="sm" c="dimmed">
              Signed in as
            </Text>
            <Title order={3} data-testid="me-name">
              {me.name}
            </Title>
            <Text c="dimmed">{me.email}</Text>
            <Badge mt="xs" variant="light" data-testid="me-tenant">
              {me.tenant.name}
            </Badge>
          </Stack>

          {loading && !summary && <DashboardTilesSkeleton />}
          {error && !summary && <DashboardTilesError message={error} onRetry={reload} />}
          {summary && <DashboardTiles summary={summary} />}

          <Button
            size="lg"
            fullWidth
            onClick={() => navigate("/assets")}
            data-testid="nav-assets"
          >
            Browse Assets
          </Button>

          <Button
            size="lg"
            fullWidth
            variant="light"
            onClick={() => navigate("/stock")}
            data-testid="nav-stock"
          >
            Stock &amp; Consumables
          </Button>

          <Button
            size="lg"
            fullWidth
            variant="light"
            onClick={() => navigate("/reservations")}
            data-testid="nav-reservations"
          >
            Reservations
          </Button>

          <Button
            size="lg"
            fullWidth
            variant="light"
            onClick={() => navigate("/approvals")}
            data-testid="nav-approvals"
          >
            Approvals
          </Button>

          <Button
            size="lg"
            fullWidth
            variant="light"
            onClick={() => navigate("/my-items")}
            data-testid="nav-my-items"
          >
            My Items
          </Button>

          <Group grow>
            <Button
              variant="light"
              onClick={() => navigate("/notifications")}
              data-testid="nav-notifications"
            >
              Notifications
            </Button>
            {hasAuditViewPermission(me) && (
              <Button
                variant="light"
                onClick={() => navigate("/audit")}
                data-testid="nav-audit"
              >
                Audit Log
              </Button>
            )}
          </Group>

          <Stack gap="xs">
            <Text fw={600}>Memberships</Text>
            {me.memberships.length === 0 && (
              <Text c="dimmed" size="sm">
                No memberships.
              </Text>
            )}
            {me.memberships.map((m, idx) => (
              <Group key={idx} gap="xs">
                <Badge variant="outline">{m.role_name}</Badge>
                <Text size="sm" c="dimmed">
                  {m.project_name ? `Project: ${m.project_name}` : "Tenant-wide"}
                </Text>
              </Group>
            ))}
          </Stack>

          <Stack gap="xs">
            <Text fw={600}>Admin</Text>
            <Group gap="xs" wrap="wrap">
              <Button variant="light" size="xs" onClick={() => navigate("/admin/categories")}>
                Categories &amp; Fields
              </Button>
              <Button variant="light" size="xs" onClick={() => navigate("/admin/locations")}>
                Locations
              </Button>
            </Group>
            <Text size="xs" c="dimmed">
              Visible to anyone who can view the inventory tree; write actions
              inside are gated by <code>category.manage</code>/
              <code>location.manage</code> (presentation only — the server is
              the real authority).
            </Text>
          </Stack>
        </Stack>
      </AppShell.Main>
    </AppShell>
  );
}
