import { MessageSquarePlus, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import type { SpaceSummary } from '../spaces/types'

import { useChat } from './ChatContext'

/**
 * Modal for creating a new conversation. Lets the caller override which
 * space the new thread anchors to (defaulting to the current anchor
 * space from ``ChatContext``). The default reflects the sidebar picker
 * so single-click "new" flows are still one action.
 */
export function NewConversationDialog({
  onClose,
  open,
}: {
  onClose: () => void
  open: boolean
}) {
  const { t } = useTranslation()
  const { anchorSpace, accessibleSpaces, createNewConversation } = useChat()

  const defaultId = anchorSpace?.id ?? accessibleSpaces[0]?.id ?? ''
  const [selected, setSelected] = useState<string>('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const effectiveSelected = useMemo(() => {
    if (selected && accessibleSpaces.some((s) => s.id === selected)) {
      return selected
    }
    return defaultId
  }, [selected, defaultId, accessibleSpaces])

  if (!open) return null

  const canSubmit = effectiveSelected && !submitting

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await createNewConversation(effectiveSelected)
      setSelected('')
      onClose()
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.detail || `HTTP ${err.status}`
          : String(err),
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card">
        <header className="dialog-header">
          <h2>
            <MessageSquarePlus size={16} /> {t('chat.newDialog.title')}
          </h2>
          <button
            aria-label={t('chat.newDialog.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form className="dialog-form" onSubmit={submit}>
          <p className="dialog-hint">{t('chat.newDialog.hint')}</p>
          <label>
            <span>{t('chat.newDialog.picker')}</span>
            <select
              onChange={(e) => setSelected(e.target.value)}
              value={effectiveSelected}
            >
              {accessibleSpaces.length === 0 && (
                <option value="">{t('chat.newDialog.empty')}</option>
              )}
              {accessibleSpaces.map((s: SpaceSummary) => (
                <option key={s.id} value={s.id}>
                  {s.name} · {s.slug}
                </option>
              ))}
            </select>
          </label>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('chat.newDialog.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              <MessageSquarePlus size={14} />
              <span>{t('chat.newDialog.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
