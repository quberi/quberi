import type { OrgUnitNode, OrgUnitTreeItem } from './types'

/**
 * Convert a flat list into a tree rooted at units without a ``parent_id`` (or
 * whose parent is not in the list — that shouldn't happen for our current
 * data but we guard against it). Nodes at the same level are sorted
 * alphabetically by name for a stable UI.
 */
export function buildTree(items: OrgUnitTreeItem[]): OrgUnitNode[] {
  const byId = new Map<string, OrgUnitNode>()
  for (const item of items) {
    byId.set(item.id, { ...item, children: [], depth: 0 })
  }

  const roots: OrgUnitNode[] = []
  for (const node of byId.values()) {
    const parent = node.parent_id ? byId.get(node.parent_id) : undefined
    if (parent) {
      parent.children.push(node)
    } else {
      roots.push(node)
    }
  }

  const compare = (a: OrgUnitNode, b: OrgUnitNode) => a.name.localeCompare(b.name)
  const decorate = (nodes: OrgUnitNode[], depth: number) => {
    nodes.sort(compare)
    for (const n of nodes) {
      n.depth = depth
      decorate(n.children, depth + 1)
    }
  }
  decorate(roots, 0)
  return roots
}
