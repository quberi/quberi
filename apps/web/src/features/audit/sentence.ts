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
