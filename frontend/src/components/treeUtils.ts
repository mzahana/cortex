/**
 * Generic helper for assembling a self-referential tree (Category or
 * Location, both `{ id, parent }` shaped per `docs/data-model.md`) from the
 * flat list the API returns. Shared by every admin tree screen (T1.5).
 */

export interface TreeNode<T> {
  id: number;
  name: string;
  parent: number | null;
  /** The original API record, so callers can read type-specific fields
   * (e.g. `Category.requires_approval`) without the tree component needing
   * to know about them. */
  raw: T;
  children: TreeNode<T>[];
}

interface TreeSource {
  id: number;
  name: string;
  parent: number | null;
}

/**
 * Builds root-level `TreeNode[]` from a flat list. Handles the tree being
 * assembled incrementally (e.g. after a create/delete refetches the flat
 * list) — this is a pure function of whatever flat list is passed in, no
 * hidden state. An item whose `parent` id isn't present in `items` (a
 * dangling reference — shouldn't happen given server-side FK integrity, but
 * defensive) is treated as a root rather than silently dropped, so nothing
 * ever disappears from the tree.
 */
export function buildTree<T extends TreeSource>(items: T[]): TreeNode<T>[] {
  const nodeById = new Map<number, TreeNode<T>>();
  for (const item of items) {
    nodeById.set(item.id, { id: item.id, name: item.name, parent: item.parent, raw: item, children: [] });
  }

  const roots: TreeNode<T>[] = [];
  for (const node of nodeById.values()) {
    if (node.parent !== null && nodeById.has(node.parent)) {
      nodeById.get(node.parent)!.children.push(node);
    } else {
      roots.push(node);
    }
  }

  const sortByName = (nodes: TreeNode<T>[]) => {
    nodes.sort((a, b) => a.name.localeCompare(b.name));
    nodes.forEach((n) => sortByName(n.children));
  };
  sortByName(roots);

  return roots;
}

/**
 * Flattens a tree into `{ value, label }` pairs for a Mantine `Select`
 * "parent" picker, indenting by depth so the hierarchy is visible in a flat
 * dropdown. `excludeIds` removes a node and its whole subtree (e.g. when
 * editing a node, it — and its descendants — can never legally become its
 * own parent; the server (`_validate_no_tree_cycle`) enforces this for real,
 * this is just so the picker doesn't even offer an obviously-invalid choice).
 */
export function flattenForSelect<T>(
  nodes: TreeNode<T>[],
  excludeIds: Set<number> = new Set(),
  depth = 0,
): { value: string; label: string }[] {
  const out: { value: string; label: string }[] = [];
  for (const node of nodes) {
    if (excludeIds.has(node.id)) continue;
    out.push({ value: String(node.id), label: `${"— ".repeat(depth)}${node.name}` });
    out.push(...flattenForSelect(node.children, excludeIds, depth + 1));
  }
  return out;
}

/** Flattens a tree back to `[id, ...ancestorIds]` for cycle-safe "can this
 * node be its own descendant's parent" client-side checks (defense in depth
 * — the server (`_validate_no_tree_cycle`) is the real guard). */
export function collectDescendantIds<T>(node: TreeNode<T>): Set<number> {
  const ids = new Set<number>();
  const walk = (n: TreeNode<T>) => {
    for (const child of n.children) {
      ids.add(child.id);
      walk(child);
    }
  };
  walk(node);
  return ids;
}
