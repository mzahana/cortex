import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActionIcon,
  Alert,
  AppShell,
  Badge,
  Button,
  Center,
  Grid,
  Group,
  Loader,
  Stack,
  Text,
  Title,
  Tooltip,
} from "@mantine/core";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import { hasPermission, CATEGORY_MANAGE } from "../../api/permissions";
import { useAuth } from "../../hooks/useAuth";
import type { Category } from "../../api/types";
import { Tree } from "../../components/Tree";
import { buildTree, type TreeNode } from "../../components/treeUtils";
import { ConfirmDeleteModal } from "../../components/ConfirmDeleteModal";
import { CategoryFormModal } from "./CategoryFormModal";
import { CategoryFieldsPanel } from "./CategoryFieldsPanel";

/**
 * Admin: Categories & Fields (T1.5, docs/api-and-ui.md "Admin: Categories &
 * Fields" screen). Builds the whole tree client-side (see `fetchAllPages`
 * doc comment in `api/client.ts` for why that's fine here — bounded admin
 * config, not an asset list) then lets an admin create/rename/move/delete
 * nodes and set the approval/consumable/calibration flags, plus manage each
 * category's custom field defs.
 *
 * Permission gating is presentation-only (CLAUDE.md): a user without
 * `category.manage` sees the same tree read-only — every write action is
 * hidden, but a stray 403 from the server (if this ever drifts from the
 * server's own check) is handled the same as any other `ApiError`, not
 * treated as a bug.
 */
export function CategoriesScreen() {
  const { me } = useAuth();
  const navigate = useNavigate();
  const canManage = hasPermission(me, CATEGORY_MANAGE);

  const [categories, setCategories] = useState<Category[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Category | null>(null);
  const [presetParentId, setPresetParentId] = useState<number | null>(null);

  const [deleteTarget, setDeleteTarget] = useState<Category | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const all = await api.listAllCategories({ ordering: "name" });
      setCategories(all);
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

  const tree = useMemo<TreeNode<Category>[]>(() => buildTree(categories ?? []), [categories]);

  const selectedCategory = useMemo(
    () => (categories ?? []).find((c) => c.id === selectedId) ?? null,
    [categories, selectedId],
  );

  const openCreate = (parentId: number | null) => {
    setEditing(null);
    setPresetParentId(parentId);
    setFormOpen(true);
  };

  const openEdit = (category: Category) => {
    setEditing(category);
    setPresetParentId(null);
    setFormOpen(true);
  };

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <ActionIcon variant="subtle" aria-label="Back" onClick={() => navigate("/")}>
              &#8592;
            </ActionIcon>
            <Title order={4}>Categories &amp; Fields</Title>
          </Group>
          {!canManage && (
            <Badge variant="light" color="gray">
              Read-only
            </Badge>
          )}
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        {categories === null && !loadError && (
          <Center p="xl">
            <Loader />
          </Center>
        )}

        {loadError && (
          <Alert color="red" mb="md" data-testid="categories-load-error">
            {loadError}
          </Alert>
        )}

        {categories !== null && (
          <Grid>
            <Grid.Col span={{ base: 12, md: 6 }}>
              <Stack gap="sm">
                <Group justify="space-between">
                  <Text fw={600}>Category tree</Text>
                  {canManage && (
                    <Button size="xs" onClick={() => openCreate(null)}>
                      Add root category
                    </Button>
                  )}
                </Group>

                <Tree<Category>
                  nodes={tree}
                  selectedId={selectedId}
                  onSelect={(node) => setSelectedId(node.id)}
                  emptyMessage="No categories yet. Create the first one to get started."
                  renderLabel={(node) => (
                    <Group gap={6} wrap="wrap">
                      <Text fw={selectedId === node.id ? 600 : 400}>{node.name}</Text>
                      {node.raw.default_is_consumable && (
                        <Badge size="xs" variant="dot" color="teal">
                          consumable
                        </Badge>
                      )}
                      {node.raw.requires_approval && (
                        <Badge size="xs" variant="dot" color="orange">
                          approval
                        </Badge>
                      )}
                      {node.raw.requires_calibration && (
                        <Badge size="xs" variant="dot" color="grape">
                          calibration
                        </Badge>
                      )}
                    </Group>
                  )}
                  renderActions={
                    canManage
                      ? (node) => (
                          <>
                            <Tooltip label="Add child category">
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
            </Grid.Col>

            <Grid.Col span={{ base: 12, md: 6 }}>
              {selectedCategory ? (
                <CategoryFieldsPanel
                  category={selectedCategory}
                  canManage={canManage}
                  onFieldAdded={() => void load()}
                />
              ) : (
                <Text c="dimmed" size="sm" p="md">
                  Select a category to view/manage its custom fields.
                </Text>
              )}
            </Grid.Col>
          </Grid>
        )}
      </AppShell.Main>

      <CategoryFormModal
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
          title="Delete category"
          itemLabel={deleteTarget.name}
          onClose={() => setDeleteTarget(null)}
          onConfirm={async () => {
            await api.deleteCategory(deleteTarget.id);
          }}
          onDeleted={() => {
            if (selectedId === deleteTarget.id) setSelectedId(null);
            void load();
          }}
        />
      )}
    </AppShell>
  );
}
