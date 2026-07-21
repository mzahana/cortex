import { Center, Loader, Tabs } from "@mantine/core";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import { ReorderRequestsPanel } from "../stock/ReorderRequestsPanel";
import { ReservationApprovalsPanel } from "./ReservationApprovalsPanel";

/**
 * Approvals screen (T3.4, `docs/api-and-ui.md`: "Pending reservation/reorder
 * approvals in my scope"). Two tabs, each a scope-aware server-filtered list:
 * pending reservations (`GET /reservations?status=pending`) and open reorder
 * requests (`GET /reorder-requests?status=open`, reusing the Stock screen's
 * `ReorderRequestsPanel` — same approve/reject/transition actions, just
 * defaulted to the actionable status here instead of "all").
 */
export function ApprovalsScreen() {
  const { me } = useAuth();

  if (!me) {
    return (
      <AppLayout title="Approvals">
        <Center h="60vh">
          <Loader />
        </Center>
      </AppLayout>
    );
  }

  return (
    <AppLayout title="Approvals">
      <Tabs defaultValue="reservations">
        <Tabs.List>
          <Tabs.Tab value="reservations">Reservations</Tabs.Tab>
          <Tabs.Tab value="reorders">Reorder requests</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="reservations" pt="md">
          <ReservationApprovalsPanel me={me} />
        </Tabs.Panel>

        <Tabs.Panel value="reorders" pt="md">
          <ReorderRequestsPanel me={me} defaultStatus="open" />
        </Tabs.Panel>
      </Tabs>
    </AppLayout>
  );
}
