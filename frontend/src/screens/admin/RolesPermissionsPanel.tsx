import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Group,
  Loader,
  Modal,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { IconPlus, IconRotate, IconTrash } from "@tabler/icons-react";
import { api, ApiError } from "../../api/client";
import type { PermissionCatalogEntry, Role } from "../../api/types";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";

/** Human-facing section headings for the `group` prefix the server derives
 * from each permission key (`asset.create` -> `asset`). Unknown groups fall
 * back to the raw prefix, so a new backend key never breaks this screen. */
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

function groupLabel(group: string): string {
  return GROUP_LABELS[group] ?? group;
}

/**
 * Admin -> Users & Roles -> "Roles & permissions" (docs/rbac.md §6).
 *
 * `docs/rbac.md` §3's matrix is the DEFAULT, not a hard-coded law: an Admin
 * edits any role's permission set here (the motivating case being a Project
 * Lead who also needs `category.manage`), and can author custom roles for
 * anything the four system roles don't cover.
 *
 * Gating is presentation-only (CLAUDE.md) — the caller renders this panel only
 * for a tenant-wide `tenant.manage` holder, and the server re-checks every
 * write regardless (`apps.rbac.api.RolePermissionClass`). A 403, or the
 * server's "this would leave nobody able to administer the tenant" 400, is a
 * normal handled outcome surfaced in the error banner, not a bug.
 *
 * Saving sends the FULL `permission_keys` set: an unchecked box is a
 * revocation, which is exactly the semantics a checkbox matrix implies.
 */
export function RolesPermissionsPanel() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [catalog, setCatalog] = useState<PermissionCatalogEntry[]>([]);
  const [selectedRoleId, setSelectedRoleId] = useState<number | null>(null);
  const [draft, setDraft] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Role | null>(null);
  const [newRoleName, setNewRoleName] = useState("");
  const [cloneFromId, setCloneFromId] = useState<string | null>(null);

  const toMessage = (err: unknown): string =>
    err instanceof ApiError
      ? (err.problem.detail ?? err.problem.title)
      : "Unable to reach the server. Please try again.";

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [rolePage, catalogPage] = await Promise.all([
        api.listRoles(),
        api.listPermissionCatalog(),
      ]);
      setRoles(rolePage.results);
      setCatalog(catalogPage.results);
      // Seed selection AND draft together, synchronously with the data that
      // produced them. Deriving the draft from a later effect keyed on the
      // selected role left a render where the boxes were all unchecked, which
      // a fast click could toggle from the wrong baseline.
      setSelectedRoleId((current) => {
        const next = current ?? rolePage.results[0]?.id ?? null;
        const role = rolePage.results.find((r) => r.id === next);
        setDraft(new Set(role?.permission_keys ?? []));
        return next;
      });
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const selectedRole = useMemo(
    () => roles.find((role) => role.id === selectedRoleId) ?? null,
    [roles, selectedRoleId],
  );

  /** Switching roles re-seeds the draft from that role's saved set, so one
   * role's unsaved checkboxes never carry over to another. */
  const selectRole = (roleId: number | null) => {
    setSelectedRoleId(roleId);
    setDraft(new Set(roles.find((role) => role.id === roleId)?.permission_keys ?? []));
    setNotice(null);
  };

  const grouped = useMemo(() => {
    const byGroup = new Map<string, PermissionCatalogEntry[]>();
    for (const entry of catalog) {
      const list = byGroup.get(entry.group) ?? [];
      list.push(entry);
      byGroup.set(entry.group, list);
    }
    return Array.from(byGroup.entries()).sort(([a], [b]) =>
      groupLabel(a).localeCompare(groupLabel(b)),
    );
  }, [catalog]);

  const dirty = useMemo(() => {
    if (!selectedRole) return false;
    const saved = new Set(selectedRole.permission_keys);
    if (saved.size !== draft.size) return true;
    for (const key of draft) if (!saved.has(key)) return true;
    return false;
  }, [draft, selectedRole]);

  const toggle = (key: string) => {
    setDraft((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const replaceRole = (updated: Role) => {
    setRoles((prev) => prev.map((role) => (role.id === updated.id ? updated : role)));
    // The server is authoritative about what actually persisted (a reset, or
    // a save the serializer normalized) — re-seed from its response.
    setDraft(new Set(updated.permission_keys));
  };

  const handleSave = async () => {
    if (!selectedRole) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.updateRole(selectedRole.id, {
        permission_keys: Array.from(draft),
      });
      replaceRole(updated);
      setNotice(`Saved. ${updated.name} now has ${updated.permission_keys.length} permissions.`);
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!selectedRole) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await api.resetRole(selectedRole.id);
      replaceRole(updated);
      setNotice(`${updated.name} restored to its default permissions.`);
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const handleCreate = async () => {
    setSaving(true);
    setError(null);
    try {
      const source = roles.find((role) => String(role.id) === cloneFromId);
      const created = await api.createRole({
        key: newRoleName,
        name: newRoleName.trim(),
        permission_keys: source ? source.permission_keys : [],
      });
      setRoles((prev) => [...prev, created].sort((a, b) => a.name.localeCompare(b.name)));
      setSelectedRoleId(created.id);
      setDraft(new Set(created.permission_keys));
      setCreateOpen(false);
      setNewRoleName("");
      setCloneFromId(null);
    } catch (err) {
      setError(toMessage(err));
    } finally {
      setSaving(false);
    }
  };

  // Deliberately lets errors propagate: `ConfirmDeleteModal` renders the
  // server's message (including the 400 "still assigned to a user" case) in
  // place, which is closer to the action than this panel's banner.
  const handleDelete = async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    await api.deleteRole(target.id);
    setRoles((prev) => prev.filter((role) => role.id !== target.id));
    setSelectedRoleId((current) => (current === target.id ? null : current));
  };

  if (loading) {
    return (
      <Group justify="center" py="xl">
        <Loader />
      </Group>
    );
  }

  return (
    <Stack gap="md">
      {error && (
        <Alert color="red" withCloseButton onClose={() => setError(null)} data-testid="roles-error">
          {error}
        </Alert>
      )}
      {notice && (
        <Alert color="teal" withCloseButton onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}

      <Group justify="space-between" wrap="wrap">
        <Select
          label="Role"
          data={roles.map((role) => ({ value: String(role.id), label: role.name }))}
          value={selectedRoleId ? String(selectedRoleId) : null}
          onChange={(value) => selectRole(value ? Number(value) : null)}
          allowDeselect={false}
          w={260}
          data-testid="role-select"
        />
        <Group gap="xs" mt="lg">
          <Button
            size="sm"
            variant="default"
            leftSection={<IconPlus size={16} />}
            onClick={() => setCreateOpen(true)}
          >
            New role
          </Button>
          {selectedRole?.is_system && selectedRole.is_customized && (
            <Button
              size="sm"
              variant="default"
              leftSection={<IconRotate size={16} />}
              onClick={() => void handleReset()}
              loading={saving}
            >
              Reset to defaults
            </Button>
          )}
          {selectedRole && !selectedRole.is_system && (
            <Button
              size="sm"
              variant="light"
              color="red"
              leftSection={<IconTrash size={16} />}
              onClick={() => setDeleteTarget(selectedRole)}
            >
              Delete role
            </Button>
          )}
        </Group>
      </Group>

      {selectedRole && (
        <Group gap="xs">
          {selectedRole.is_system ? (
            <Badge variant="light">System role</Badge>
          ) : (
            <Badge variant="light" color="grape">
              Custom role
            </Badge>
          )}
          {selectedRole.is_customized && (
            <Badge variant="light" color="orange">
              Edited from defaults
            </Badge>
          )}
          <Text size="sm" c="dimmed">
            {selectedRole.member_count} member{selectedRole.member_count === 1 ? "" : "s"}
          </Text>
        </Group>
      )}

      <Text size="sm" c="dimmed">
        Unchecking a box revokes that permission for everyone holding this role. Project-scoped
        roles only ever apply within the project a membership is scoped to.
      </Text>

      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="sm">
        {grouped.map(([group, entries]) => (
          <Card withBorder key={group} padding="sm">
            <Title order={6} mb="xs">
              {groupLabel(group)}
            </Title>
            <Stack gap={6}>
              {entries.map((entry) => (
                <Checkbox
                  key={entry.key}
                  size="sm"
                  label={entry.label}
                  description={entry.key}
                  checked={draft.has(entry.key)}
                  onChange={() => toggle(entry.key)}
                  data-testid={`perm-${entry.key}`}
                />
              ))}
            </Stack>
          </Card>
        ))}
      </SimpleGrid>

      <Group justify="flex-end">
        <Button
          variant="default"
          disabled={!dirty || saving}
          onClick={() => setDraft(new Set(selectedRole?.permission_keys ?? []))}
        >
          Discard changes
        </Button>
        <Button
          disabled={!dirty}
          loading={saving}
          onClick={() => void handleSave()}
          data-testid="save-role-permissions"
        >
          Save permissions
        </Button>
      </Group>

      <Modal opened={createOpen} onClose={() => setCreateOpen(false)} title="New role" centered>
        <Stack gap="sm">
          <TextInput
            label="Role name"
            placeholder="Lab Tech"
            value={newRoleName}
            onChange={(event) => setNewRoleName(event.currentTarget.value)}
            data-testid="new-role-name"
          />
          <Select
            label="Start from"
            description="Copy another role's permissions as a starting point (optional)"
            data={roles.map((role) => ({ value: String(role.id), label: role.name }))}
            value={cloneFromId}
            onChange={setCloneFromId}
            clearable
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button
              loading={saving}
              disabled={!newRoleName.trim()}
              onClick={() => void handleCreate()}
            >
              Create role
            </Button>
          </Group>
        </Stack>
      </Modal>

      <ConfirmDeleteModal
        opened={deleteTarget !== null}
        title="Delete role"
        itemLabel={deleteTarget?.name ?? ""}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleDelete}
        onDeleted={() => setDeleteTarget(null)}
      >
        <Text size="sm" c="dimmed">
          A role still assigned to any user cannot be deleted — reassign those memberships
          first.
        </Text>
      </ConfirmDeleteModal>
    </Stack>
  );
}
