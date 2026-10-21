import type { TFunction } from 'i18next'

import type { RecentEvent } from './types'

/**
 * One human sentence for an audit event, e.g. "Alice uploaded handbook.docx".
 *
 * Every action code has an ``audit.recent.action.<code>`` template. A code
 * without one falls back to a generic "actor · action · resource" so a newly
 * added server-side action is never invisible.
 */
export function eventSentence(t: TFunction, event: RecentEvent): string {
  const actor = event.actor_display_name || t('audit.recent.unknownActor')
  const fallback = t('audit.recent.action.fallback', {
    actor,
    action: event.action,
    resource: event.resource_display_name,
  })
  return t(`audit.recent.action.${event.action}`, {
    actor,
    resource: event.resource_display_name,
    space: event.space_display_name ?? '',
    defaultValue: fallback,
  })
}

/**
 * Events that only record somebody looking at something or chatting, not a change to the
 * organisation. The Overview feed hides them by default so the changes that matter (access
 * granted, a member deactivated, a rule added, a file uploaded) are not buried under them.
 */
const LOOK_ONLY_ACTIONS = new Set([
  'document.access_checked',
  'space.documents_listed_for_access',
  'knowledge.external_search',
  'knowledge.external_image_search',
  'knowledge.external_list',
  'conversation.created',
  'conversation.renamed',
  'message.sent',
  'message.send_cancelled',
  'document.upload_duplicate',
  'knowledge.external_upload_duplicate',
])

export function isLookOnlyAction(action: string): boolean {
  return LOOK_ONLY_ACTIONS.has(action)
}
