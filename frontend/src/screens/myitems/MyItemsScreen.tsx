import {
  ActionIcon,
  Alert,
  AppShell,
  Button,
  Center,
  Group,
  Loader,
  Pagination,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useNavigate } from "react-router-dom";
import type { Checkout } from "../../api/types";
import { useMyItemsList } from "./useMyItemsList";
import { CheckoutRow } from "./CheckoutRow";

/**
 * My Items screen (T3.5, `docs/api-and-ui.md` "My Items": "What I have out,
 * due dates, overdue; quick check-in"). Server-side paginated
 * `GET /api/v1/checkouts?open=true` — the server already scopes this to the
 * caller's own open checkouts (union'd with any `checkout.manage`/
 * `checkout.override` scope they separately hold), so this screen shows
 * exactly "what I have out" without a client-side `user` filter.
 */
export function MyItemsScreen() {
  const navigate = useNavigate();
  const { items, assetsById, totalCount, page, pageCount, loading, error, setPage, reload } =
    useMyItemsList();

  const handleCheckedIn = (updated: Checkout) => {
    void updated;
    reload();
  };

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/")}>
              &#8592;
            </ActionIcon>
            <Title order={4}>My Items</Title>
          </Group>
          <Text size="sm" c="dimmed">
            {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
          </Text>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Stack gap="sm">
          {error && (
            <Alert color="red" title="Couldn't load your items">
              <Stack gap="xs" align="flex-start">
                <Text size="sm">{error}</Text>
                <Button size="xs" variant="light" onClick={reload}>
                  Retry
                </Button>
              </Stack>
            </Alert>
          )}

          {loading && !error && (
            <Center p="xl">
              <Loader data-testid="my-items-loading" />
            </Center>
          )}

          {!loading && !error && items.length === 0 && (
            <Center p="xl">
              <Stack align="center" gap={4}>
                <Text fw={600}>Nothing checked out</Text>
                <Text size="sm" c="dimmed">
                  Items you check out will show up here with their due dates.
                </Text>
              </Stack>
            </Center>
          )}

          {!loading && !error && items.length > 0 && (
            <Stack gap="sm" data-testid="my-items-list">
              {items.map((checkout) => (
                <CheckoutRow
                  key={checkout.id}
                  checkout={checkout}
                  asset={assetsById.get(checkout.asset)}
                  onCheckedIn={handleCheckedIn}
                />
              ))}
            </Stack>
          )}

          {pageCount > 1 && (
            <Center>
              <Pagination total={pageCount} value={page} onChange={setPage} size="sm" />
            </Center>
          )}
        </Stack>
      </AppShell.Main>
    </AppShell>
  );
}
