import { Alert, Button, CopyButton, Group, Modal, Stack, Text, TextInput } from "@mantine/core";
import { IconCheck, IconCopy } from "@tabler/icons-react";

interface CreatedPasswordModalProps {
  opened: boolean;
  email: string;
  password: string;
  onClose: () => void;
}

/**
 * One-time reveal for a newly-generated user password (`POST /api/v1/users/`
 * response's `password` field — see `CreatedUser` doc comment in
 * `api/types.ts`). The server returns this value exactly once and never
 * again; this modal is the ONLY place it is ever displayed. Security
 * invariants (task requirement / CLAUDE.md "no secrets in the audit log"):
 * never logged, never put in a URL/query param, and held only in the
 * caller's transient React state for exactly as long as this modal is open
 * — closing it is the caller's cue to drop that state entirely.
 */
export function CreatedPasswordModal({
  opened,
  email,
  password,
  onClose,
}: CreatedPasswordModalProps) {
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title="User created"
      centered
      closeOnClickOutside={false}
      data-testid="created-password-modal"
    >
      <Stack gap="sm">
        <Text size="sm">
          Account created for <strong>{email}</strong>.
        </Text>
        <Alert color="yellow" title="Copy this now — it will not be shown again">
          This initial password is shown exactly once. Share it with the user securely (e.g. in
          person or over a trusted channel) and have them change it after they log in.
        </Alert>
        <TextInput
          label="Initial password"
          value={password}
          readOnly
          data-testid="created-password-value"
          styles={{ input: { fontFamily: "monospace" } }}
        />
        <Group justify="flex-end">
          <CopyButton value={password} timeout={2000}>
            {({ copied, copy }) => (
              <Button
                variant={copied ? "filled" : "light"}
                color={copied ? "teal" : undefined}
                leftSection={copied ? <IconCheck size={16} /> : <IconCopy size={16} />}
                onClick={copy}
                data-testid="copy-password-button"
              >
                {copied ? "Copied" : "Copy password"}
              </Button>
            )}
          </CopyButton>
          <Button onClick={onClose} data-testid="created-password-done">
            Done
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
