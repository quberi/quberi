import { ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useAuth } from '../auth/AuthContext'
import { listPendingSpaces, type PendingSpace } from './reviewApi'

/** Spaces I review that have uploads waiting. Shows nothing when there are none. */
export function PendingReviewInbox({ onOpenSpace }: { onOpenSpace: (spaceId: string) => void }) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [items, setItems] = useState<PendingSpace[]>([])

  useEffect(() => {
    if (!credentials || credentials.isGuest) return
    listPendingSpaces(credentials)
      .then((list) => setItems(list.items))
      .catch(() => setItems([]))
  }, [credentials])

  if (items.length === 0) return null
  return (
    <section className="panel pending-review-inbox">
      <span className="section-label">
        <ShieldCheck size={14} /> {t('uploadReview.inboxTitle')}
      </span>
      <ul className="access-request-list">
        {items.map((s) => (
          <li key={s.space_id}>
            <div className="access-request-actions">
              <strong>{t('uploadReview.inboxLine', { space: s.space_name, count: s.pending })}</strong>
              <button className="primary-button" onClick={() => onOpenSpace(s.space_id)} type="button">
                {t('uploadReview.inboxOpen')}
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
