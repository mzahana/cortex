import type { ReactNode } from "react";
import { useState } from "react";
import { Alert, Button, Group, Modal, Text } from "@mantine/core";
import { ApiError } from "../api/client";

interface ConfirmDeleteModalProps {
  opened: boolean;
  title: string;
  itemLabel: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
  onDeleted: () => void;
  /** Optional extra content rendered above the standard confirm text — e.g.
   * a stronger destructive/cascade warning for deletes that remove more
   * than just the one row (see `CategoryFieldsPanel`'s field-delete, which
   * cascades to every stored `AssetFieldValue`). */
  children?: ReactNode;
}

/**
 * Shared delete-confirm modal for the admin tree screens. Surfaces the
 * documented `409 Conflict` (`ProtectedError` — a `PROTECT` FK blocks the
 * delete, e.g. a category/location with children) as a clear "still in use"
 * message rather than a generic error (T1.5 acceptance: "protected delete
 * (409) -> show a clear 'in use, can't delete' message").
 */
export function ConfirmDeleteModal({
  opened,
  title,
  itemLabel,
  onClose,
  onConfirm,
  onDeleted,
  children,
}: ConfirmDeleteModalProps) {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleConfirm = async () => {
    setError(null);
    setDeleting(true);
    try {
      await onConfirm();
      onDeleted();
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError(
          err.problem.detail ??
            "Can't delete: this item is still in use (it has children or other records referencing it).",
        );
      } else if (err instanceof ApiError) {
        setError(err.problem.detail ?? err.problem.title);
      } else {
        setError("Unable to reach the server. Please try again.");
      }
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Modal opened={opened} onClose={onClose} title={title} centered>
      {error && (
        <Alert color="red" mb="sm" data-testid="delete-error">
          {error}
        </Alert>
      )}
      {children}
      <Text size="sm" mb="md">
        Delete <strong>{itemLabel}</strong>? This can&apos;t be undone.
      </Text>
      <Group justify="flex-end">
        <Button variant="default" onClick={onClose}>
          Cancel
        </Button>
        <Button color="red" loading={deleting} onClick={() => void handleConfirm()}>
          Delete
        </Button>
      </Group>
    </Modal>
  );
}
