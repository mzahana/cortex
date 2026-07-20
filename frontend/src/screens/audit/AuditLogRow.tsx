import { Badge, Card, Code, Group, Stack, Text } from "@mantine/core";
import type { AuditLogEntry } from "../../api/types";

/** One audit log entry, expanded inline (no detail route — the list row
 * already carries the full before/after snapshot, `apps.audit.serializers.
 * AuditLogSerializer`'s fields are identical on list vs. retrieve). */
export function AuditLogRow({ entry }: { entry: AuditLogEntry }) {
  return (
    <Card withBorder padding="md" radius="md" data-testid={`audit-row-${entry.id}`}>
      <Stack gap="xs">
        <Group justify="space-between" wrap="wrap">
          <Group gap="xs" wrap="wrap">
            <Badge variant="filled">{entry.action}</Badge>
            <Badge variant="outline">
              {entry.entity_type} #{entry.entity_id}
            </Badge>
          </Group>
          <Text size="xs" c="dimmed">
            {new Date(entry.created_at).toLocaleString()}
          </Text>
        </Group>

        <Text size="sm">
          {entry.actor_name || entry.actor_email
            ? `${entry.actor_name ?? "—"} (${entry.actor_email ?? "no email"})`
            : "System"}
          {entry.ip ? ` · ${entry.ip}` : ""}
        </Text>

        {(entry.before !== null || entry.after !== null) && (
          // Stacked, not side-by-side — a two-column layout for JSON blobs
          // is unreadable at the mobile-first widths this app targets
          // (CLAUDE.md: "every screen must be usable on a phone").
          <Stack gap="xs">
            <Stack gap={2}>
              <Text size="xs" fw={600} c="dimmed">
                Before
              </Text>
              <Code block style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                {JSON.stringify(entry.before, null, 2) ?? "null"}
              </Code>
            </Stack>
            <Stack gap={2}>
              <Text size="xs" fw={600} c="dimmed">
                After
              </Text>
              <Code block style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                {JSON.stringify(entry.after, null, 2) ?? "null"}
              </Code>
            </Stack>
          </Stack>
        )}
      </Stack>
    </Card>
  );
}
