import { useState } from "react";
import { Alert, Button, Group, Modal, Stack, Text } from "@mantine/core";
import { api, ApiError } from "../../api/client";
import type { CreatedUser } from "../../api/types";

interface ResetUserPasswordTarget {
  userId: number;
  email: string;
}

interface ResetUserPasswordModalProps {
  target: ResetUserPasswordTarget | null;
  onClose: () => void;
  /** Called with the one-time `CreatedUser` (carries the fresh `password`)
   * once the reset succeeds — the parent reveals it via `CreatedPasswordModal`. */
  onReset: (user: CreatedUser) => void;
}

/**
 * Admin confirmation before regenerating another user's password
 * (`POST /api/v1/users/{id}/reset-password/`, Admin-only tenant-wide
 * `user.manage`). Resetting immediately invalidates the user's current
 * password, so this gates the action behind an explicit confirm. A server
 * 403 (e.g. a ProjectLead who slipped past the presentation gate) is surfaced
 * inline as a normal, handled outcome.
 */
export function ResetUserPasswordModal({
  target,
  onClose,
  onReset,
}: ResetUserPasswordModalProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleConfirm = async () => {
    if (!target) return;
    setError(null);
    setSubmitting(true);
    try {
      const created = await api.resetUserPassword(target.userId);
      onReset(created);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      opened={!!target}
      onClose={onClose}
      title="Reset password"
      centered
      data-testid="reset-password-confirm-modal"
    >
      <Stack gap="sm">
        <Text size="sm">
          Generate a new one-time password for <strong>{target?.email}</strong>? Their current
          password will stop working immediately, and you&apos;ll need to share the new one with
          them securely.
        </Text>
        {error && (
          <Alert color="red" data-testid="reset-password-confirm-error">
            {error}
          </Alert>
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            color="orange"
            onClick={handleConfirm}
            loading={submitting}
            data-testid="reset-password-confirm"
          >
            Reset password
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
