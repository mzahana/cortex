import { useState, type ReactNode } from "react";
import {
  ActionIcon,
  AppShell,
  Avatar,
  Box,
  Drawer,
  Group,
  NavLink,
  ScrollArea,
  Stack,
  Text,
  Title,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { useMediaQuery } from "@mantine/hooks";
import { IconDots, IconLogout } from "@tabler/icons-react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { MOBILE_MORE_ITEMS, MOBILE_TABS, NAV_ITEMS, type NavItem } from "./nav";

function isNavItemActive(pathname: string, to: string): boolean {
  if (to === "/") return pathname === "/";
  return pathname === to || pathname.startsWith(`${to}/`);
}

/** Buckets nav items into consecutive runs by `section` (desktop sidebar
 * only — mobile's tab bar / "More" sheet stay flat lists), preserving
 * `NAV_ITEMS`' order. `undefined` section (just Dashboard) comes back as one
 * ungrouped, caption-less run first. */
function groupNavItems(items: NavItem[]): Array<[NavItem["section"], NavItem[]]> {
  const groups: Array<[NavItem["section"], NavItem[]]> = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && last[0] === item.section) {
      last[1].push(item);
    } else {
      groups.push([item.section, [item]]);
    }
  }
  return groups;
}

interface AppLayoutProps {
  /** Current screen's title, shown in the shared top bar (replaces every
   * screen's own ad hoc `<AppShell.Header>` title). */
  title: ReactNode;
  /** Page-level primary action(s) (e.g. "New asset"), rendered on the right
   * of the top bar. */
  actions?: ReactNode;
  /** When set, renders a back arrow before the title that navigates here —
   * for drill-down screens reached from a list (Asset Detail, forms), not
   * top-level nav destinations. */
  backTo?: string;
  children: ReactNode;
}

/**
 * Shared app chrome (T4/nav redesign): a persistent left sidebar on desktop/
 * tablet, collapsing to a bottom tab bar + "More" sheet on mobile — replaces
 * every screen's own repeated `<AppShell>` + back-arrow header. See
 * `layout/nav.ts` for the nav item list and the mobile tab-bar selection.
 */
export function AppLayout({ title, actions, backTo, children }: AppLayoutProps) {
  const { me, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  // `false` default (desktop-first) avoids a layout flash on first paint for
  // the common desktop dev/test viewport; the real value settles within a
  // frame on any device.
  const isMobile = useMediaQuery("(max-width: 767px)", false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);

  const visibleNavItems = NAV_ITEMS.filter((item) => !item.hidden?.(me));
  const visibleMoreItems = MOBILE_MORE_ITEMS.filter((item) => !item.hidden?.(me));

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await logout();
    } catch {
      // `logout()` is best-effort and swallows its own errors — defense in
      // depth only, same as the original DashboardScreen handler.
    } finally {
      setLoggingOut(false);
      navigate("/login", { replace: true });
    }
  };

  const goTo = (to: string) => {
    setMoreOpen(false);
    navigate(to);
  };

  return (
    <AppShell
      header={{ height: 60 }}
      navbar={!isMobile ? { width: 268, breakpoint: "sm" } : undefined}
      footer={isMobile ? { height: 76 } : undefined}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
            {backTo && (
              <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate(backTo)}>
                &#8592;
              </ActionIcon>
            )}
            {isMobile && !backTo && (
              <Text fw={800} c="brand" size="sm" style={{ letterSpacing: 0.4 }}>
                CORTEX
              </Text>
            )}
            <Title order={4} lineClamp={1} style={{ minWidth: 0 }}>
              {title}
            </Title>
          </Group>
          {actions && (
            <Group gap="xs" wrap="nowrap">
              {actions}
            </Group>
          )}
        </Group>
      </AppShell.Header>

      {!isMobile && (
        <AppShell.Navbar p="md">
          <Stack justify="space-between" h="100%" gap="md">
            <Stack gap="lg">
              <Group gap="xs" px={4}>
                <Box
                  w={32}
                  h={32}
                  bg="brand.6"
                  style={{
                    borderRadius: "var(--mantine-radius-md)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: "white",
                    fontWeight: 800,
                    fontSize: 14,
                    flexShrink: 0,
                  }}
                >
                  CX
                </Box>
                <Stack gap={0}>
                  <Text fw={800} size="sm" style={{ letterSpacing: 0.4 }}>
                    CORTEX
                  </Text>
                  <Text size="xs" c="dimmed">
                    Lab Inventory
                  </Text>
                </Stack>
              </Group>

              <ScrollArea.Autosize mah="calc(100vh - 260px)" type="auto">
                <Stack gap="md">
                  {groupNavItems(visibleNavItems).map(([section, items]) => (
                    <Stack gap={2} key={section ?? "_ungrouped"}>
                      {section && (
                        <Text
                          size="10px"
                          fw={700}
                          c="dimmed"
                          px={8}
                          pt={4}
                          style={{ letterSpacing: 0.6, textTransform: "uppercase" }}
                        >
                          {section}
                        </Text>
                      )}
                      {items.map((item) => (
                        <NavLink
                          key={item.to}
                          label={item.label}
                          leftSection={<item.icon size={18} stroke={1.75} />}
                          active={isNavItemActive(location.pathname, item.to)}
                          onClick={() => navigate(item.to)}
                          data-testid={item.testId}
                          variant="filled"
                          style={{ borderRadius: "var(--mantine-radius-md)" }}
                        />
                      ))}
                    </Stack>
                  ))}
                </Stack>
              </ScrollArea.Autosize>
            </Stack>

            <Box>
              <Group
                justify="space-between"
                wrap="nowrap"
                p="xs"
                style={{
                  borderRadius: "var(--mantine-radius-md)",
                  border: "1px solid var(--mantine-color-default-border)",
                }}
              >
                <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
                  <Avatar radius="xl" color="brand" size="sm">
                    {(me?.name ?? "?").slice(0, 1).toUpperCase()}
                  </Avatar>
                  <Stack gap={0} style={{ minWidth: 0 }}>
                    <Text size="sm" fw={600} lineClamp={1} data-testid="me-name">
                      {me?.name}
                    </Text>
                    <Text size="xs" c="dimmed" lineClamp={1} data-testid="me-tenant">
                      {me?.tenant.name}
                    </Text>
                  </Stack>
                </Group>
                <Tooltip label="Log out">
                  <ActionIcon
                    variant="subtle"
                    color="gray"
                    aria-label="Log out"
                    loading={loggingOut}
                    onClick={() => void handleLogout()}
                  >
                    <IconLogout size={16} stroke={1.75} />
                  </ActionIcon>
                </Tooltip>
              </Group>
              <Text ta="center" c="dimmed" size="10px" mt="xs">
                Cortex v{__APP_VERSION__} · © {new Date().getFullYear()} Mohamed Abdelkader ·
                Apache-2.0
              </Text>
            </Box>
          </Stack>
        </AppShell.Navbar>
      )}

      <AppShell.Main>
        <Box
          pb={isMobile ? 8 : 0}
          style={{ ["--app-bottom-nav-offset" as string]: isMobile ? "76px" : "0px" }}
        >
          {children}
        </Box>
      </AppShell.Main>

      {isMobile && (
        <AppShell.Footer>
          <MobileTabBar
            pathname={location.pathname}
            onNavigate={goTo}
            onMore={() => setMoreOpen(true)}
          />
        </AppShell.Footer>
      )}

      {isMobile && (
        <Drawer
          opened={moreOpen}
          onClose={() => setMoreOpen(false)}
          position="bottom"
          title="More"
          size="auto"
          radius="lg"
        >
          <Stack gap="md" pb="sm">
            <Group
              justify="space-between"
              wrap="nowrap"
              p="xs"
              style={{
                borderRadius: "var(--mantine-radius-md)",
                border: "1px solid var(--mantine-color-default-border)",
              }}
            >
              <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
                <Avatar radius="xl" color="brand" size="sm">
                  {(me?.name ?? "?").slice(0, 1).toUpperCase()}
                </Avatar>
                <Stack gap={0} style={{ minWidth: 0 }}>
                  <Text size="sm" fw={600} lineClamp={1} data-testid="me-name">
                    {me?.name}
                  </Text>
                  <Text size="xs" c="dimmed" lineClamp={1} data-testid="me-tenant">
                    {me?.tenant.name}
                  </Text>
                </Stack>
              </Group>
            </Group>
            <Stack gap={2}>
            {visibleMoreItems.map((item) => (
              <NavLink
                key={item.to}
                label={item.label}
                leftSection={<item.icon size={18} stroke={1.75} />}
                active={isNavItemActive(location.pathname, item.to)}
                onClick={() => goTo(item.to)}
                data-testid={item.testId}
                style={{ borderRadius: "var(--mantine-radius-md)" }}
              />
            ))}
            <NavLink
              label="Log out"
              leftSection={<IconLogout size={18} stroke={1.75} />}
              onClick={() => void handleLogout()}
              style={{ borderRadius: "var(--mantine-radius-md)" }}
            />
            </Stack>
            <Text ta="center" c="dimmed" size="10px">
              Cortex v{__APP_VERSION__} · © {new Date().getFullYear()} Mohamed Abdelkader ·
              Apache-2.0
            </Text>
          </Stack>
        </Drawer>
      )}
    </AppShell>
  );
}

function MobileTabBar({
  pathname,
  onNavigate,
  onMore,
}: {
  pathname: string;
  onNavigate: (to: string) => void;
  onMore: () => void;
}) {
  return (
    <Group h="100%" px="xs" justify="space-around" wrap="nowrap" gap={0}>
      {MOBILE_TABS.map((item) => {
        const active = isNavItemActive(pathname, item.to);
        // The Scan tab (idx 2, dead-center of 4) is the app's signature
        // one-tap action (T4.3) — visually elevated as a filled circular
        // button so it stays "always one tap away" the way the old FAB was,
        // even though it now lives inside the tab bar instead of floating.
        const isScan = item.to === "/scan";
        return (
          <UnstyledButton
            key={item.to}
            onClick={() => onNavigate(item.to)}
            data-testid={item.testId}
            aria-label={item.label}
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              gap: 2,
              flex: 1,
              paddingTop: isScan ? 0 : 6,
              color: active
                ? "var(--mantine-color-brand-6)"
                : "var(--mantine-color-dimmed)",
            }}
          >
            {isScan ? (
              <Box
                w={48}
                h={48}
                mt={-18}
                bg="brand.6"
                style={{
                  borderRadius: "50%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "white",
                  boxShadow: "var(--mantine-shadow-md)",
                }}
              >
                <item.icon size={22} stroke={1.9} />
              </Box>
            ) : (
              <item.icon size={22} stroke={active ? 2.1 : 1.75} />
            )}
            <Text size="10px" fw={active ? 700 : 500} style={{ color: "inherit" }}>
              {item.label}
            </Text>
          </UnstyledButton>
        );
      })}
      <UnstyledButton
        onClick={onMore}
        aria-label="More"
        data-testid="tab-more"
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 2,
          flex: 1,
          paddingTop: 6,
          color: "var(--mantine-color-dimmed)",
        }}
      >
        <IconDots size={22} stroke={1.75} />
        <Text size="10px" fw={500}>
          More
        </Text>
      </UnstyledButton>
    </Group>
  );
}

export type { NavItem };
