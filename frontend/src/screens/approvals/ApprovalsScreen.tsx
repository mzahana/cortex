import { ActionIcon, AppShell, Center, Group, Loader, Tabs, Title } from "@mantine/core";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../hooks/useAuth";
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
  const navigate = useNavigate();
  const { me } = useAuth();

  if (!me) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md">
          <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/")}>
            &#8592;
          </ActionIcon>
          <Title order={4}>Approvals</Title>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
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
      </AppShell.Main>
    </AppShell>
  );
}
