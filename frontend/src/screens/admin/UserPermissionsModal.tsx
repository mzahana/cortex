import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Loader,
  Modal,
  SegmentedControl,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { api, ApiError } from "../../api/client";
import type {
  PermissionCatalogEntry,
  PermissionOverrideEffect,
  UserPermissions,
} from "../../api/types";

const GROUP_LABELS: Record<string, string> = {
  asset: "Assets",
  category: "Categories & fields",
  location: "Locations",
  stock: "Stock",
  reorder: "Reorder requests",
  reservation: "Reservations",
  checkout: "Check-out / check-in",
  issue: "Issues",
  maintenance: "Maintenance",
  label: "Labels",
  import: "Import / export",
  user: "Users",
  role: "Roles",
  audit: "Audit",
  tenant: "Lab settings",
  notify: "Notifications",
  project: "Projects",
  expense: "Project finances",
};

type Tri = "inherit" | PermissionOverrideEffect;

/**
 * Admin: per-user permission overrides (docs/rbac.md §6) — the "this one
 * person needs a deviation from their role" adjustment, without authoring a
 * whole custom role for it.
 *
 * Tri-state per permission:
 * - **Inherit** — whatever the user's roles say (the default; no override row).
 * - **Always allow** — a tenant-wide GRANT on top of their roles.
 * - **Never allow** — a tenant-wide DENY that beats every grant, including a
 *   permission they only hold through a project-scoped membership.
 *
 * Overrides are tenant-wide by construction (see `UserPermissionOverride`'s
 * model docstring for why they deliberately have no project scope), so the
 * copy here says "everywhere" rather than implying per-project control.
 *
 * Saving PUTs the whole map — anything back on Inherit is simply omitted,
 * which is what deletes its override row server-side.
 */
export function UserPermissionsModal({
  opened,
  userId,
  userEmail,
  onClose,
}: {
  opened: boolean;
  userId: number | null;
  userEmail: string;
  onClose: () => void;
}) {
  const [catalog, setCatalog] = useState<PermissionCatalogEntry[]>([]);
  const [data, setData] = useState<UserPermissions | null>(null);
  const [draft, setDraft] = useState<Record<string, Tri>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const toMessage = (err: unknown): string =>
    err instanceof ApiError
      ? (err.problem.detail ?? err.problem.title)
      : "Unable to reach the server. Please try again.";

  const load = useCallback(async () => {
    if (userId === null) return;
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const [catalogPage, permissions] = await Promise.all([
        api.listPermissionCatalog(),
        api.getUserPermissions(userId),
      ]);
      setCatalog(catalogPage.results);
      setData(permissions);
      setDraft(
        Object.fromEntries(
          catalogPage.results.map((entry) => [
            entry.key,
            (permissions.overrides[entry.key] ?? "inherit") as Tri,
          ]),
        ),
      );
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    if (opened) void load();
  }, [opened, load]);

  const grouped = useMemo(() => {
    const byGroup = new Map<string, PermissionCatalogEntry[]>();
    for (const entry of catalog) {
      const list = byGroup.get(entry.group) ?? [];
      list.push(entry);
      byGroup.set(entry.group, list);
    }
    return Array.from(byGroup.entries()).sort(([a], [b]) =>
      (GROUP_LABELS[a] ?? a).localeCompare(GROUP_LABELS[b] ?? b),
    );
  }, [catalog]);

  const roleKeys = useMemo(
    () => new Set(data?.role_permission_keys ?? []),
    [data?.role_permission_keys],
  );

  const overrideCount = useMemo(
    () => Object.values(draft).filter((value) => value !== "inherit").length,
    [draft],
  );

  const handleSave = async () => {
    if (userId === null) return;
    setSaving(true);
    setError(null);
    try {
      const overrides = Object.fromEntries(
        Object.entries(draft).filter(([, value]) => value !== "inherit"),
      ) as Record<string, PermissionOverrideEffect>;
      const updated = await api.updateUserPermissions(userId, overrides);
      setData(updated);
      setNotice("Saved. The user sees the change the next time their session refreshes.");
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={`Permissions — ${userEmail}`}
      size="xl"
      centered
    >
      <Stack gap="md">
        {error && (
          <Alert color="red" withCloseButton onClose={() => setError(null)} data-testid="user-perms-error">
            {error}
          </Alert>
        )}
        {notice && (
          <Alert color="teal" withCloseButton onClose={() => setNotice(null)}>
            {notice}
          </Alert>
        )}

        {loading ? (
          <Group justify="center" py="xl">
            <Loader />
          </Group>
        ) : (
          <>
            <Text size="sm" c="dimmed">
              Overrides apply everywhere in the lab and sit on top of this user&apos;s roles.
              &ldquo;Never allow&rdquo; wins over any role grant, including one they only hold
              inside a project.
            </Text>
            {overrideCount > 0 && (
              <Badge variant="light" color="orange">
                {overrideCount} override{overrideCount === 1 ? "" : "s"}
              </Badge>
            )}

            {grouped.map(([group, entries]) => (
              <Card withBorder key={group} padding="sm">
                <Title order={6} mb="xs">
                  {GROUP_LABELS[group] ?? group}
                </Title>
                <Stack gap="xs">
                  {entries.map((entry) => (
                    <Group key={entry.key} justify="space-between" wrap="nowrap" gap="sm">
                      <div style={{ minWidth: 0 }}>
                        <Text size="sm">{entry.label}</Text>
                        <Text size="xs" c="dimmed">
                          {entry.key}
                          {roleKeys.has(entry.key) ? " · granted by role" : ""}
                        </Text>
                      </div>
                      <SegmentedControl
                        size="xs"
                        value={draft[entry.key] ?? "inherit"}
                        onChange={(value) =>
                          setDraft((prev) => ({ ...prev, [entry.key]: value as Tri }))
                        }
                        data={[
                          { value: "inherit", label: "Inherit" },
                          { value: "grant", label: "Allow" },
                          { value: "deny", label: "Deny" },
                        ]}
                        data-testid={`override-${entry.key}`}
                      />
                    </Group>
                  ))}
                </Stack>
              </Card>
            ))}

            <Group justify="flex-end">
              <Button variant="default" onClick={onClose}>
                Close
              </Button>
              <Button
                loading={saving}
                onClick={() => void handleSave()}
                data-testid="save-user-permissions"
              >
                Save overrides
              </Button>
            </Group>
          </>
        )}
      </Stack>
    </Modal>
  );
}
