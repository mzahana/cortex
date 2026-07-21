import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  Stack,
  Text,
  Tooltip,
} from "@mantine/core";
import { api, ApiError } from "../../api/client";
import { hasPermission, LOCATION_MANAGE } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import { AppLayout } from "../../layout/AppLayout";
import type { Location } from "../../api/types";
import { Tree } from "../../components/Tree";
import { buildTree, type TreeNode } from "../../components/treeUtils";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";
import { LocationFormModal } from "./LocationFormModal";

/**
 * Admin: Locations (T1.5, docs/api-and-ui.md "Admin: Locations" screen).
 * Same tree-editor pattern as `CategoriesScreen`, simpler model (no
 * per-node side panel — `Location` has no nested custom-field-def concept).
 */
export function LocationsScreen() {
  const { me } = useAuth();
  const canManage = hasPermission(me, LOCATION_MANAGE);

  const [locations, setLocations] = useState<Location[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Location | null>(null);
  const [presetParentId, setPresetParentId] = useState<number | null>(null);

  const [deleteTarget, setDeleteTarget] = useState<Location | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const all = await api.listAllLocations({ ordering: "name" });
      setLocations(all);
    } catch (err) {
      setLoadError(
        err instanceof ApiError
          ? err.problem.detail ?? err.problem.title
          : "Unable to reach the server. Please try again.",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const tree = useMemo<TreeNode<Location>[]>(() => buildTree(locations ?? []), [locations]);

  const openCreate = (parentId: number | null) => {
    setEditing(null);
    setPresetParentId(parentId);
    setFormOpen(true);
  };

  const openEdit = (location: Location) => {
    setEditing(location);
    setPresetParentId(null);
    setFormOpen(true);
  };

  return (
    <AppLayout
      title="Locations"
      actions={
        !canManage ? (
          <Badge variant="light" color="gray">
            Read-only
          </Badge>
        ) : undefined
      }
    >
        {locations === null && !loadError && (
          <Center p="xl">
            <Loader />
          </Center>
        )}

        {loadError && (
          <Alert color="red" mb="md" data-testid="locations-load-error">
            {loadError}
          </Alert>
        )}

        {locations !== null && (
          <Stack gap="sm">
            <Group justify="space-between">
              <Text fw={600}>Location tree</Text>
              {canManage && (
                <Button size="xs" onClick={() => openCreate(null)}>
                  Add root location
                </Button>
              )}
            </Group>

            <Tree<Location>
              nodes={tree}
              emptyMessage="No locations yet. Create the first one (e.g. a Room) to get started."
              renderLabel={(node) => (
                <Group gap={6} wrap="wrap">
                  <Text>{node.name}</Text>
                  {node.raw.kind && (
                    <Badge size="xs" variant="light">
                      {node.raw.kind}
                    </Badge>
                  )}
                </Group>
              )}
              renderActions={
                canManage
                  ? (node) => (
                      <>
                        <Tooltip label="Add child location">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            aria-label={`Add child of ${node.name}`}
                            onClick={() => openCreate(node.id)}
                          >
                            +
                          </ActionIcon>
                        </Tooltip>
                        <Tooltip label="Edit">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            aria-label={`Edit ${node.name}`}
                            onClick={() => openEdit(node.raw)}
                          >
                            ✎
                          </ActionIcon>
                        </Tooltip>
                        <Tooltip label="Delete">
                          <ActionIcon
                            variant="subtle"
                            size="sm"
                            color="red"
                            aria-label={`Delete ${node.name}`}
                            onClick={() => setDeleteTarget(node.raw)}
                          >
                            🗑
                          </ActionIcon>
                        </Tooltip>
                      </>
                    )
                  : undefined
              }
            />
          </Stack>
        )}

      <LocationFormModal
        opened={formOpen}
        onClose={() => setFormOpen(false)}
        onSaved={() => void load()}
        tree={tree}
        editing={editing}
        presetParentId={presetParentId}
      />

      {deleteTarget && (
        <ConfirmDeleteModal
          opened={!!deleteTarget}
          title="Delete location"
          itemLabel={deleteTarget.name}
          onClose={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await api.deleteLocation(deleteTarget.id);
          }}
          onDeleted={() => void load()}
        />
      )}
    </AppLayout>
  );
}
