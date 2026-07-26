import { useCallback, useEffect, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  Pagination,
  Stack,
  Table,
  Text,
  Tooltip,
} from "@mantine/core";
import { IconKey, IconTrash, IconUserPlus, IconUsersGroup } from "@tabler/icons-react";
import { api, ApiError } from "../../api/client";
import { hasPermission, hasUserManagePermission, USER_MANAGE } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";
import type { CreatedUser, Membership, Project, Role } from "../../api/types";
import { AddMemberModal } from "./AddMemberModal";
import { ChangeRoleModal } from "./ChangeRoleModal";
import { CreateUserModal } from "./CreateUserModal";
import { CreatedPasswordModal } from "./CreatedPasswordModal";
import { ResetUserPasswordModal } from "./ResetUserPasswordModal";
import { useMembershipList } from "./useMembershipList";

/**
 * Admin: "Users & Roles" — the previously-missing screen for the account/
 * membership management the backend has fully supported since M5
 * (`apps.rbac.api.MembershipViewSet`) plus the `user.manage` gap-fill
 * endpoints (`apps.accounts.api.UserViewSet`, `apps.rbac.api.RoleViewSet`).
 *
 * Presentation-only gating (CLAUDE.md): the nav entry and this screen's
 * write affordances are hidden without `user.manage` in any scope, but the
 * server is the sole authority — a 403 from any of these actions is a
 * normal, handled outcome, not a bug. An Admin (tenant-wide `user.manage`)
 * sees/grants across the whole tenant; a ProjectLead sees/grants only
 * within their own project(s) (server-enforced narrowing, both on the list
 * query and on what `POST /api/v1/memberships/` will actually accept).
 */
export function UsersRolesScreen() {
  const { me } = useAuth();
  const canManage = hasUserManagePermission(me);
  // Resetting another user's password is Admin-only (tenant-wide `user.manage`,
  // enforced server-side) — gate the affordance the same way so a ProjectLead
  // isn't shown an action that will 403.
  const canResetPassword = hasPermission(me, USER_MANAGE);

  const { items, totalCount, page, pageCount, loading, error, forbidden, setPage, reload } =
    useMembershipList();

  const [roles, setRoles] = useState<Role[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [refDataError, setRefDataError] = useState<string | null>(null);

  const [addMemberOpen, setAddMemberOpen] = useState(false);
  const [createUserOpen, setCreateUserOpen] = useState(false);
  const [changeRoleTarget, setChangeRoleTarget] = useState<Membership | null>(null);
  const [removeTarget, setRemoveTarget] = useState<Membership | null>(null);
  const [createdUser, setCreatedUser] = useState<CreatedUser | null>(null);
  const [resetTarget, setResetTarget] = useState<{ userId: number; email: string } | null>(null);
  // Distinguishes the reveal copy: a fresh account vs. a reset of an existing one.
  const [passwordModalKind, setPasswordModalKind] = useState<"created" | "reset">("created");

  const loadRefData = useCallback(async () => {
    if (!canManage) return;
    setRefDataError(null);
    try {
      const [roleBody, projectList] = await Promise.all([api.listRoles(), api.listAllProjects()]);
      setRoles(roleBody.results);
      setProjects(projectList);
    } catch (err) {
      setRefDataError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    }
  }, [canManage]);

  useEffect(() => {
    void loadRefData();
  }, [loadRefData]);

  return (
    <AppLayout
      title="Users & Roles"
      actions={
        canManage ? (
          <Group gap="xs" wrap="nowrap">
            <Button
              size="xs"
              variant="default"
              leftSection={<IconUserPlus size={16} />}
              onClick={() => setCreateUserOpen(true)}
              data-testid="open-create-user"
            >
              New user
            </Button>
            <Button
              size="xs"
              leftSection={<IconUsersGroup size={16} />}
              onClick={() => setAddMemberOpen(true)}
              data-testid="open-add-member"
            >
              Add member
            </Button>
          </Group>
        ) : (
          <Badge variant="light" color="gray">
            Read-only
          </Badge>
        )
      }
    >
      <Stack gap="md">
        {refDataError && (
          <Alert color="red" data-testid="ref-data-error">
            {refDataError}
          </Alert>
        )}

        {error && (
          <Alert color="red" title={forbidden ? "Not available" : "Couldn't load members"}>
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
            <Loader data-testid="memberships-loading" />
          </Center>
        )}

        {!loading && !error && items.length === 0 && (
          <Center p="xl">
            <Text c="dimmed">No members yet.</Text>
          </Center>
        )}

        {!loading && !error && items.length > 0 && (
          <Table.ScrollContainer minWidth={480}>
            <Table striped highlightOnHover data-testid="memberships-table">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>User</Table.Th>
                  <Table.Th>Role</Table.Th>
                  <Table.Th>Scope</Table.Th>
                  {canManage && <Table.Th />}
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {items.map((m) => (
                  <Table.Tr key={m.id} data-testid={`membership-row-${m.id}`}>
                    <Table.Td>{m.user_email}</Table.Td>
                    <Table.Td>
                      <Badge variant="light">{m.role_name}</Badge>
                    </Table.Td>
                    <Table.Td>
                      {m.project_name ?? <Text c="dimmed">Tenant-wide</Text>}
                    </Table.Td>
                    {canManage && (
                      <Table.Td>
                        <Group gap={4} justify="flex-end" wrap="nowrap">
                          <Tooltip label="Change role">
                            <ActionIcon
                              variant="subtle"
                              size="sm"
                              aria-label={`Change role for ${m.user_email}`}
                              onClick={() => setChangeRoleTarget(m)}
                              data-testid={`membership-change-role-${m.id}`}
                            >
                              ✎
                            </ActionIcon>
                          </Tooltip>
                          {canResetPassword && (
                            <Tooltip label="Reset password">
                              <ActionIcon
                                variant="subtle"
                                size="sm"
                                aria-label={`Reset password for ${m.user_email}`}
                                onClick={() =>
                                  setResetTarget({ userId: m.user, email: m.user_email })
                                }
                                data-testid={`membership-reset-password-${m.id}`}
                              >
                                <IconKey size={16} />
                              </ActionIcon>
                            </Tooltip>
                          )}
                          <Tooltip label="Remove">
                            <ActionIcon
                              variant="subtle"
                              size="sm"
                              color="red"
                              aria-label={`Remove ${m.user_email}`}
                              onClick={() => setRemoveTarget(m)}
                              data-testid={`membership-remove-${m.id}`}
                            >
                              <IconTrash size={16} />
                            </ActionIcon>
                          </Tooltip>
                        </Group>
                      </Table.Td>
                    )}
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}

        {totalCount !== null && (
          <Text size="xs" c="dimmed">
            {items.length} of {totalCount}
          </Text>
        )}

        {pageCount > 1 && (
          <Center>
            <Pagination total={pageCount} value={page} onChange={setPage} size="sm" />
          </Center>
        )}
      </Stack>

      <AddMemberModal
        opened={addMemberOpen}
        onClose={() => setAddMemberOpen(false)}
        roles={roles}
        projects={projects}
        onAdded={reload}
        onUserCreated={(user) => {
          setPasswordModalKind("created");
          setCreatedUser(user);
        }}
      />

      <CreateUserModal
        opened={createUserOpen}
        onClose={() => setCreateUserOpen(false)}
        onCreated={(user) => {
          setCreateUserOpen(false);
          setPasswordModalKind("created");
          setCreatedUser(user);
        }}
      />

      <ResetUserPasswordModal
        target={resetTarget}
        onClose={() => setResetTarget(null)}
        onReset={(user) => {
          setResetTarget(null);
          setPasswordModalKind("reset");
          setCreatedUser(user);
        }}
      />

      <CreatedPasswordModal
        opened={!!createdUser}
        email={createdUser?.email ?? ""}
        password={createdUser?.password ?? ""}
        onClose={() => setCreatedUser(null)}
        title={passwordModalKind === "reset" ? "Password reset" : "User created"}
        intro={
          passwordModalKind === "reset" ? (
            <>
              New password generated for <strong>{createdUser?.email}</strong>.
            </>
          ) : undefined
        }
      />

      <ChangeRoleModal
        membership={changeRoleTarget}
        roles={roles}
        onClose={() => setChangeRoleTarget(null)}
        onChanged={reload}
      />

      {removeTarget && (
        <ConfirmDeleteModal
          opened={!!removeTarget}
          title="Remove member"
          itemLabel={`${removeTarget.user_email} (${removeTarget.role_name}, ${
            removeTarget.project_name ?? "Tenant-wide"
          })`}
          onClose={() => setRemoveTarget(null)}
          onConfirm={async () => {
            await api.deleteMembership(removeTarget.id);
          }}
          onDeleted={reload}
        />
      )}
    </AppLayout>
  );
}
