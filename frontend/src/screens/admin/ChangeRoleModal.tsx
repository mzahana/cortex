import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Group, Modal, Select, Stack, Text } from "@mantine/core";
import { api, ApiError } from "../../api/client";
import type { Membership, Role } from "../../api/types";

interface ChangeRoleModalProps {
  membership: Membership | null;
  roles: Role[];
  onClose: () => void;
  onChanged: () => void;
}

/**
 * Per-membership "change role" action -> `PATCH /api/v1/memberships/{id}/`
 * (`role.assign`, audited server-side with the OLD and NEW role key —
 * `apps.rbac.api.MembershipViewSet.perform_update`). `user`/`project` are
 * fixed once a Membership exists (server-enforced, `MembershipSerializer.
 * get_fields`) — this modal only ever changes `role`.
 */
export function ChangeRoleModal({ membership, roles, onClose, onChanged }: ChangeRoleModalProps) {
  const [roleId, setRoleId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setRoleId(membership ? String(membership.role) : null);
    setError(null);
  }, [membership]);

  const roleOptions = useMemo(
    () => roles.map((r) => ({ value: String(r.id), label: r.name })),
    [roles],
  );

  const handleSubmit = async () => {
    if (!membership || !roleId) return;
    setError(null);
    setSaving(true);
    try {
      await api.updateMembership(membership.id, { role: Number(roleId) });
      onChanged();
      onClose();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal opened={!!membership} onClose={onClose} title="Change role" centered>
      {membership && (
        <Stack gap="sm">
          {error && (
            <Alert color="red" data-testid="change-role-error">
              {error}
            </Alert>
          )}
          <Text size="sm">
            {membership.user_email} — {membership.project_name ?? "Tenant-wide"}
          </Text>
          <Select
            label="Role"
            data={roleOptions}
            value={roleId}
            onChange={setRoleId}
            data-testid="change-role-select"
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose} disabled={saving}>
              Cancel
            </Button>
            <Button
              loading={saving}
              disabled={!roleId || roleId === String(membership.role)}
              onClick={() => void handleSubmit()}
              data-testid="change-role-submit"
            >
              Save
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}
