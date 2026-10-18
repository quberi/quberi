import type { SpaceSummary } from '../spaces/types'

/**
 * Whether the person may only look at this space's documents. Adding and deleting are then
 * not offered, since the server would refuse them: a reader sees a button that never works.
 *
 * ``caller_role`` is the person's role in the space; a space where only some documents were
 * shared with them has no role and nothing they can change. Guests are handled by the sandbox
 * rules, not here, and an unknown role is not treated as read-only (the server decides).
 */
export function isReadOnlySpace(
  space: Pick<SpaceSummary, 'caller_role' | 'shared_only_documents'> | null | undefined,
  isGuest: boolean,
): boolean {
  if (isGuest || !space) return false
  if ((space.shared_only_documents ?? 0) > 0) return true
  return space.caller_role === 'viewer'
}
