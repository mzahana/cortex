import { useEffect, useMemo, useState } from "react";
import { useDebouncedValue } from "@mantine/hooks";
import {
  Alert,
  Button,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Select,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { api, ApiError } from "../../api/client";
import type { AppUser, CreatedUser, Project, Role } from "../../api/types";
import { CreateUserModal } from "./CreateUserModal";

interface AddMemberModalProps {
  opened: boolean;
  onClose: () => void;
  roles: Role[];
  projects: Project[];
  onAdded: () => void;
  /** Bubbled up so the parent screen can show the one-time password reveal
   * (`CreatedPasswordModal`) — this modal never renders the password
   * itself, same separation `CreateUserModal` keeps. */
  onUserCreated: (user: CreatedUser) => void;
}

const TENANT_WIDE_VALUE = "__tenant_wide__";

/**
 * "Add member" flow: user picker backed by `GET /api/v1/users/` — with no
 * search text it lists current users (ordered by name) so one can be picked
 * directly; typing filters that same list via `?search=`. Role picker (`GET
 * /api/v1/roles/`, already loaded by the parent), project picker (`GET
 * /api/v1/projects/`, already loaded by the parent, plus a synthetic
 * "Tenant-wide" option = no project) -> `POST /api/v1/memberships/`. If no
 * existing user matches the search, an inline "create new user" link opens
 * `CreateUserModal`; once that succeeds, the newly-created user is
 * auto-selected here so the admin can finish granting the membership in one
 * flow.
 */
export function AddMemberModal({
  opened,
  onClose,
  roles,
  projects,
  onAdded,
  onUserCreated,
}: AddMemberModalProps) {
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebouncedValue(search, 300);
  const [userResults, setUserResults] = useState<AppUser[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [selectedUser, setSelectedUser] = useState<AppUser | null>(null);
  const [roleId, setRoleId] = useState<string | null>(null);
  const [projectValue, setProjectValue] = useState<string>(TENANT_WIDE_VALUE);

  const [saving, setSaving] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const [createUserOpen, setCreateUserOpen] = useState(false);

  useEffect(() => {
    if (!opened) return;
    // Reset transient state each time the modal is (re)opened, so a
    // previous grant's leftovers never bleed into the next one.
    setSearch("");
    setUserResults([]);
    setSelectedUser(null);
    setRoleId(null);
    setProjectValue(TENANT_WIDE_VALUE);
    setSubmitError(null);
    setSearchError(null);
  }, [opened]);

  useEffect(() => {
    if (!opened || selectedUser) return;
    const trimmed = debouncedSearch.trim();
    let cancelled = false;
    setSearching(true);
    setSearchError(null);
    api
      .listUsers(trimmed ? { search: trimmed } : { ordering: "name" })
      .then((body) => {
        if (cancelled) return;
        setUserResults(body.results);
      })
      .catch((err) => {
        if (cancelled) return;
        setSearchError(
          err instanceof ApiError
            ? err.problem.detail ?? err.problem.title
            : "Unable to reach the server. Please try again.",
        );
      })
      .finally(() => {
        if (!cancelled) setSearching(false);
      });
    return () => {
      cancelled = true;
    };
  }, [debouncedSearch, opened, selectedUser]);

  const isBrowsing = !debouncedSearch.trim();

  const roleOptions = useMemo(
    () => roles.map((r) => ({ value: String(r.id), label: r.name })),
    [roles],
  );
  const projectOptions = useMemo(
    () => [
      { value: TENANT_WIDE_VALUE, label: "Tenant-wide (all projects)" },
      ...projects.map((p) => ({ value: String(p.id), label: p.name })),
    ],
    [projects],
  );

  const handleClose = () => {
    if (saving) return;
    onClose();
  };

  const handleSubmit = async () => {
    if (!selectedUser || !roleId) return;
    setSubmitError(null);
    setSaving(true);
    try {
      await api.createMembership({
        user: selectedUser.id,
        role: Number(roleId),
        project: projectValue === TENANT_WIDE_VALUE ? null : Number(projectValue),
      });
      onAdded();
      onClose();
    } catch (err) {
      setSubmitError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Modal opened={opened} onClose={handleClose} title="Add member" centered size="md">
        <Stack gap="sm">
          {submitError && (
            <Alert color="red" data-testid="add-member-error">
              {submitError}
            </Alert>
          )}

          {!selectedUser ? (
            <Stack gap="xs">
              <TextInput
                label="Find user"
                placeholder="Search by email or name…"
                value={search}
                onChange={(e) => setSearch(e.currentTarget.value)}
                data-testid="add-member-user-search"
              />
              {searchError && (
                <Text c="red" size="sm">
                  {searchError}
                </Text>
              )}
              {searching && (
                <Group gap="xs">
                  <Loader size="xs" />
                  <Text size="sm" c="dimmed">
                    {isBrowsing ? "Loading users…" : "Searching…"}
                  </Text>
                </Group>
              )}
              {!searching && !isBrowsing && userResults.length === 0 && !searchError && (
                <Text size="sm" c="dimmed">
                  No matching users.
                </Text>
              )}
              {!searching && isBrowsing && userResults.length === 0 && !searchError && (
                <Text size="sm" c="dimmed">
                  No users found.
                </Text>
              )}
              {userResults.length > 0 && (
                <ScrollArea.Autosize mah={220}>
                  <Stack gap={4}>
                    {isBrowsing && (
                      <Text size="xs" c="dimmed" mb={2}>
                        Current users
                      </Text>
                    )}
                    {userResults.map((u) => (
                      <Button
                        key={u.id}
                        variant="default"
                        justify="flex-start"
                        onClick={() => setSelectedUser(u)}
                        data-testid={`add-member-user-option-${u.id}`}
                      >
                        <Stack gap={0} align="flex-start">
                          <Text size="sm" fw={500}>
                            {u.name || u.email}
                          </Text>
                          {u.name && (
                            <Text size="xs" c="dimmed">
                              {u.email}
                            </Text>
                          )}
                        </Stack>
                      </Button>
                    ))}
                  </Stack>
                </ScrollArea.Autosize>
              )}
              <Button
                variant="subtle"
                size="xs"
                onClick={() => setCreateUserOpen(true)}
                data-testid="add-member-create-user"
              >
                No match — create a new user
              </Button>
            </Stack>
          ) : (
            <Group justify="space-between" wrap="nowrap">
              <Stack gap={0}>
                <Text size="sm" fw={500} data-testid="add-member-selected-user">
                  {selectedUser.name || selectedUser.email}
                </Text>
                <Text size="xs" c="dimmed">
                  {selectedUser.email}
                </Text>
              </Stack>
              <Button variant="subtle" size="xs" onClick={() => setSelectedUser(null)}>
                Change
              </Button>
            </Group>
          )}

          <Select
            label="Role"
            placeholder="Choose a role"
            data={roleOptions}
            value={roleId}
            onChange={setRoleId}
            required
            data-testid="add-member-role"
          />
          <Select
            label="Scope"
            data={projectOptions}
            value={projectValue}
            onChange={(v) => setProjectValue(v ?? TENANT_WIDE_VALUE)}
            required
            data-testid="add-member-project"
          />

          <Group justify="flex-end">
            <Button variant="default" onClick={handleClose} disabled={saving}>
              Cancel
            </Button>
            <Button
              loading={saving}
              disabled={!selectedUser || !roleId}
              onClick={() => void handleSubmit()}
              data-testid="add-member-submit"
            >
              Grant membership
            </Button>
          </Group>
        </Stack>
      </Modal>

      <CreateUserModal
        opened={createUserOpen}
        onClose={() => setCreateUserOpen(false)}
        onCreated={(created) => {
          setCreateUserOpen(false);
          setSelectedUser({ id: created.id, email: created.email, name: created.name });
          onUserCreated(created);
        }}
      />
    </>
  );
}
