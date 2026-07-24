import { useState } from "react";
import { Alert, Button, Group, Modal, Stack, TextInput } from "@mantine/core";
import { api, ApiError } from "../../api/client";
import type { CreatedUser } from "../../api/types";

interface CreateUserModalProps {
  opened: boolean;
  onClose: () => void;
  /** Called with the full `POST /api/v1/users/` response (including the
   * one-time `password`) so the caller can hand it straight to
   * `CreatedPasswordModal` — this component never renders the password
   * itself. */
  onCreated: (user: CreatedUser) => void;
}

/**
 * "Create new user" form (email + name) -> `POST /api/v1/users/`
 * (Admin-only, tenant-wide `user.manage` — a stray 403 here is a normal,
 * handled outcome per CLAUDE.md, not a bug). Reachable both standalone from
 * the Users & Roles screen and from inside `AddMemberModal`'s user picker
 * when no existing user matches a search.
 */
export function CreateUserModal({ opened, onClose, onCreated }: CreateUserModalProps) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setEmail("");
    setName("");
    setError(null);
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  const handleSubmit = async () => {
    setError(null);
    setSaving(true);
    try {
      const created = await api.createUser({ email: email.trim(), name: name.trim() });
      reset();
      onCreated(created);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.problem.detail ?? err.problem.title : "Unable to reach the server. Please try again.",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal opened={opened} onClose={handleClose} title="Create new user" centered>
      <Stack gap="sm">
        {error && (
          <Alert color="red" data-testid="create-user-error">
            {error}
          </Alert>
        )}
        <TextInput
          label="Email"
          placeholder="person@example.com"
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.currentTarget.value)}
          data-testid="create-user-email"
        />
        <TextInput
          label="Name"
          placeholder="Full name"
          value={name}
          onChange={(e) => setName(e.currentTarget.value)}
          data-testid="create-user-name"
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={handleClose}>
            Cancel
          </Button>
          <Button
            loading={saving}
            disabled={!email.trim()}
            onClick={() => void handleSubmit()}
            data-testid="create-user-submit"
          >
            Create user
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
