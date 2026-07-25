import { useState } from "react";
import { Alert, Button, Center, Loader, Pagination, SegmentedControl, Stack, Text } from "@mantine/core";
import { AppLayout } from "../../layout/AppLayout";
import type { Checkout } from "../../api/types";
import { useMyItemsList } from "./useMyItemsList";
import { CheckoutRow } from "./CheckoutRow";
import { HistoryRow } from "./HistoryRow";

/**
 * My Items screen (T3.5, `docs/api-and-ui.md` "My Items": "What I have out,
 * due dates, overdue; quick check-in"). Server-side paginated
 * `GET /api/v1/checkouts?open=true|false` — the server already scopes this
 * to the caller's own checkouts (union'd with any `checkout.manage`/
 * `checkout.override` scope they separately hold), so this screen shows
 * exactly "what I have out" / "what I've had out" without a client-side
 * `user` filter. "Current" (open=true) and "History" (open=false, already
 * checked in) are separate server-paginated queries — switching tabs resets
 * to page 1 rather than merging pages.
 */
export function MyItemsScreen() {
  const [tab, setTab] = useState<"current" | "history">("current");
  const current = useMyItemsList(true);
  const history = useMyItemsList(false);
  const { items, assetsById, totalCount, page, pageCount, loading, error, setPage, reload } =
    tab === "current" ? current : history;

  const handleCheckedIn = (updated: Checkout) => {
    void updated;
    current.reload();
    history.reload();
  };

  return (
    <AppLayout
      title="My Items"
      actions={
        <Text size="sm" c="dimmed">
          {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
        </Text>
      }
    >
        <Stack gap="sm">
          <SegmentedControl
            data-testid="my-items-tab"
            fullWidth
            value={tab}
            onChange={(value) => setTab(value as "current" | "history")}
            data={[
              { label: "Current", value: "current" },
              { label: "History", value: "history" },
            ]}
          />

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
                <Text fw={600}>
                  {tab === "current" ? "Nothing checked out" : "No history yet"}
                </Text>
                <Text size="sm" c="dimmed">
                  {tab === "current"
                    ? "Items you check out will show up here with their due dates."
                    : "Items you've checked out and returned will show up here."}
                </Text>
              </Stack>
            </Center>
          )}

          {!loading && !error && items.length > 0 && (
            <Stack gap="sm" data-testid="my-items-list">
              {items.map((checkout) =>
                tab === "current" ? (
                  <CheckoutRow
                    key={checkout.id}
                    checkout={checkout}
                    asset={assetsById.get(checkout.asset)}
                    onCheckedIn={handleCheckedIn}
                  />
                ) : (
                  <HistoryRow key={checkout.id} checkout={checkout} asset={assetsById.get(checkout.asset)} />
                ),
              )}
            </Stack>
          )}

          {pageCount > 1 && (
            <Center>
              <Pagination total={pageCount} value={page} onChange={setPage} size="sm" />
            </Center>
          )}
        </Stack>
    </AppLayout>
  );
}
