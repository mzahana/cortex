import { useMemo, useState } from "react";
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
  TextInput,
  Title,
} from "@mantine/core";
import { DateTimePicker } from "@mantine/dates";
import { useNavigate } from "react-router-dom";
import type { AuditLogListParams } from "../../api/types";
import { AuditLogRow } from "./AuditLogRow";
import { useAuditLog } from "./useAuditLog";

/**
 * Audit Log screen (T5.6, `docs/api-and-ui.md`: "Filterable immutable
 * history"). Read-only, server-side paginated (`GET /api/v1/audit`), never
 * loads "all" history (CLAUDE.md). Gated presentation-only by `audit.view`
 * (nav link in `DashboardScreen`) — a stale bookmark/direct nav for anyone
 * without the permission in any scope hits a 403 here, handled as a normal
 * outcome (CLAUDE.md), not a crash. An Admin sees every tenant entry; a
 * ProjectLead sees only their own project's entries (server-enforced,
 * `apps.audit.api.AuditLogViewSet.get_queryset` — no client-side narrowing
 * needed/possible here).
 */
export function AuditLogScreen() {
  const navigate = useNavigate();

  const [entityType, setEntityType] = useState("");
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [createdAfter, setCreatedAfter] = useState<Date | null>(null);
  const [createdBefore, setCreatedBefore] = useState<Date | null>(null);

  const filters = useMemo<AuditLogListParams>(
    () => ({
      entity_type: entityType || undefined,
      action: action || undefined,
      actor: actor ? Number(actor) : undefined,
      created_after: createdAfter ? createdAfter.toISOString() : undefined,
      created_before: createdBefore ? createdBefore.toISOString() : undefined,
      ordering: "-created_at",
    }),
    [entityType, action, actor, createdAfter, createdBefore],
  );

  const { items, totalCount, page, pageCount, loading, error, forbidden, setPage, reload } =
    useAuditLog({ filters });

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/")}>
              &#8592;
            </ActionIcon>
            <Title order={4}>Audit Log</Title>
          </Group>
          <Text size="sm" c="dimmed">
            {totalCount !== null ? `${items.length} of ${totalCount}` : ""}
          </Text>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Stack gap="md">
          <Group grow wrap="wrap">
            <TextInput
              label="Entity type"
              placeholder="e.g. asset"
              value={entityType}
              onChange={(e) => setEntityType(e.currentTarget.value)}
              data-testid="audit-filter-entity-type"
            />
            <TextInput
              label="Action"
              placeholder="e.g. asset.retire"
              value={action}
              onChange={(e) => setAction(e.currentTarget.value)}
              data-testid="audit-filter-action"
            />
            <TextInput
              label="Actor (user id)"
              placeholder="e.g. 3"
              value={actor}
              onChange={(e) => setActor(e.currentTarget.value.replace(/[^0-9]/g, ""))}
              data-testid="audit-filter-actor"
            />
          </Group>
          <Group grow wrap="wrap">
            <DateTimePicker
              label="Created after"
              value={createdAfter}
              onChange={setCreatedAfter}
              clearable
              data-testid="audit-filter-created-after"
            />
            <DateTimePicker
              label="Created before"
              value={createdBefore}
              onChange={setCreatedBefore}
              clearable
              data-testid="audit-filter-created-before"
            />
          </Group>

          {error && (
            <Alert color="red" title={forbidden ? "Not available" : "Couldn't load the audit log"}>
              <Stack gap="xs" align="flex-start">
                <Text size="sm">{error}</Text>
                {!forbidden && (
                  <Button size="xs" variant="light" onClick={reload}>
                    Retry
                  </Button>
                )}
              </Stack>
            </Alert>
          )}

          {loading && !error && (
            <Center p="xl">
              <Loader data-testid="audit-log-loading" />
            </Center>
          )}

          {!loading && !error && items.length === 0 && (
            <Center p="xl">
              <Text c="dimmed">No matching audit entries.</Text>
            </Center>
          )}

          {!loading && !error && items.length > 0 && (
            <Stack gap="sm" data-testid="audit-log-list">
              {items.map((entry) => (
                <AuditLogRow key={entry.id} entry={entry} />
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
