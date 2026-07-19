import { useState, type ReactNode } from "react";
import { ActionIcon, Box, Group, Text, UnstyledButton } from "@mantine/core";
import type { TreeNode } from "./treeUtils";

export interface TreeProps<T> {
  nodes: TreeNode<T>[];
  /** Currently-selected node id (e.g. the category whose field defs are
   * shown in a side panel). */
  selectedId?: number | null;
  onSelect?: (node: TreeNode<T>) => void;
  /** Main label content for a row (name + any type-specific badges). */
  renderLabel: (node: TreeNode<T>) => ReactNode;
  /** Trailing row actions (add-child/edit/delete). Omit entirely (return
   * `null`) to render nothing — callers gate this by their own
   * read-vs-manage permission check, this component has no opinion on
   * authorization (CLAUDE.md: UI gating is presentation only, decided by the
   * screen, not baked into this reusable primitive). */
  renderActions?: (node: TreeNode<T>) => ReactNode;
  emptyMessage?: string;
}

/**
 * Reusable self-referential tree view (Category or Location, or any future
 * `{id, name, parent}` tree). Expand/collapse per node, touch-friendly rows
 * (mobile-first — CLAUDE.md), and a pluggable label/actions renderer so
 * Category (flags/badges) and Location (kind) can each show what's relevant
 * without forking this component.
 *
 * Handles being rebuilt incrementally: it takes a plain `TreeNode<T>[]` on
 * every render (no internal copy of the data), so a parent that refetches
 * the flat list after a create/edit/delete and calls `buildTree()` again
 * just works — this component's only internal state is which node ids are
 * expanded, keyed by id, so it survives across those rebuilds too.
 */
export function Tree<T>({ nodes, selectedId, onSelect, renderLabel, renderActions, emptyMessage }: TreeProps<T>) {
  const [collapsedIds, setCollapsedIds] = useState<Set<number>>(new Set());

  const toggle = (id: number) => {
    setCollapsedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (nodes.length === 0) {
    return (
      <Text c="dimmed" size="sm" p="sm">
        {emptyMessage ?? "Nothing here yet."}
      </Text>
    );
  }

  return (
    <Box>
      {nodes.map((node) => (
        <TreeRow
          key={node.id}
          node={node}
          depth={0}
          collapsedIds={collapsedIds}
          onToggle={toggle}
          selectedId={selectedId}
          onSelect={onSelect}
          renderLabel={renderLabel}
          renderActions={renderActions}
        />
      ))}
    </Box>
  );
}

interface TreeRowProps<T> {
  node: TreeNode<T>;
  depth: number;
  collapsedIds: Set<number>;
  onToggle: (id: number) => void;
  selectedId?: number | null;
  onSelect?: (node: TreeNode<T>) => void;
  renderLabel: (node: TreeNode<T>) => ReactNode;
  renderActions?: (node: TreeNode<T>) => ReactNode;
}

function TreeRow<T>({
  node,
  depth,
  collapsedIds,
  onToggle,
  selectedId,
  onSelect,
  renderLabel,
  renderActions,
}: TreeRowProps<T>) {
  const hasChildren = node.children.length > 0;
  const collapsed = collapsedIds.has(node.id);
  const isSelected = selectedId === node.id;

  return (
    <Box>
      <Group
        wrap="nowrap"
        gap={4}
        pl={depth * 16}
        py={4}
        style={{
          borderRadius: 4,
          backgroundColor: isSelected ? "var(--mantine-color-blue-light)" : undefined,
        }}
      >
        <ActionIcon
          variant="subtle"
          size="md"
          aria-label={collapsed ? `Expand ${node.name}` : `Collapse ${node.name}`}
          onClick={() => onToggle(node.id)}
          style={{ visibility: hasChildren ? "visible" : "hidden", flexShrink: 0 }}
        >
          {collapsed ? "▸" : "▾"}
        </ActionIcon>

        <UnstyledButton
          onClick={() => onSelect?.(node)}
          style={{ flex: 1, minWidth: 0, textAlign: "left", padding: "4px 6px", borderRadius: 4 }}
        >
          {renderLabel(node)}
        </UnstyledButton>

        {renderActions && <Group gap={2} wrap="nowrap">{renderActions(node)}</Group>}
      </Group>

      {hasChildren && !collapsed && (
        <Box>
          {node.children.map((child) => (
            <TreeRow
              key={child.id}
              node={child}
              depth={depth + 1}
              collapsedIds={collapsedIds}
              onToggle={onToggle}
              selectedId={selectedId}
              onSelect={onSelect}
              renderLabel={renderLabel}
              renderActions={renderActions}
            />
          ))}
        </Box>
      )}
    </Box>
  );
}
