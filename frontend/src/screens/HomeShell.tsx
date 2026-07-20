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
import { useAuth } from "../hooks/useAuth";

/**
 * Placeholder authenticated shell (T0.7). Real screens (Home w/ Scan FAB,
 * Assets, Stock, Reservations, ...) land in later milestones; this proves
 * the `/me` round-trip and gives every future screen a place to live.
 */
export function HomeShell() {
  const { me, logout } = useAuth();
  const navigate = useNavigate();
  const [loggingOut, setLoggingOut] = useState(false);

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
            <Text fw={600}>General permissions</Text>
            <Group gap={4} wrap="wrap">
              {me.permissions.length === 0 && (
                <Text c="dimmed" size="sm">
                  None.
                </Text>
              )}
              {me.permissions.map((perm) => (
                <Badge key={perm} size="sm" variant="dot">
                  {perm}
                </Badge>
              ))}
            </Group>
          </Stack>

          {Object.keys(me.project_permissions).length > 0 && (
            <Stack gap="xs">
              <Text fw={600}>Project-scoped permissions</Text>
              {Object.entries(me.project_permissions).map(([projectId, perms]) => (
                <Stack key={projectId} gap={4}>
                  <Text size="sm" c="dimmed">
                    Project {projectId}
                  </Text>
                  <Group gap={4} wrap="wrap">
                    {perms.map((perm) => (
                      <Badge key={perm} size="sm" variant="dot" color="grape">
                        {perm}
                      </Badge>
                    ))}
                  </Group>
                </Stack>
              ))}
            </Stack>
          )}

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

          <Text size="xs" c="dimmed">
            Home (Scan FAB, asset lists, etc.) lands in later milestones — this
            is the T0.7 authenticated-shell placeholder.
          </Text>
        </Stack>
      </AppShell.Main>
    </AppShell>
  );
}
