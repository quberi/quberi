import { Ban, Share2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

/**
 * Small marks next to a document's name saying it has its own access rules: shared with
 * people outside the space, hidden from people inside it, or both. Shown only when the server
 * sent the counts, which it does for whoever manages the space and nobody else.
 */
export function ExceptionBadges({
  allow,
  deny,
}: {
  allow?: number | null
  deny?: number | null
}) {
  const { t } = useTranslation()
  const shared = allow ?? 0
  const hidden = deny ?? 0
  if (shared === 0 && hidden === 0) return null
  return (
    <span className="exception-badges">
      {shared > 0 && (
        <span className="exception-badge exception-badge-allow" title={t('docAccess.badge.allowHint')}>
          <Share2 size={10} aria-hidden="true" />
          {t('docAccessAdmin.allowCount', { count: shared })}
        </span>
      )}
      {hidden > 0 && (
        <span className="exception-badge exception-badge-deny" title={t('docAccess.badge.denyHint')}>
          <Ban size={10} aria-hidden="true" />
          {t('docAccessAdmin.denyCount', { count: hidden })}
        </span>
      )}
    </span>
  )
}
