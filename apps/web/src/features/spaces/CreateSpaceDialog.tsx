import { Loader2, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import { useSpaces } from './SpacesContext'
import type { CreateSpaceInput } from './types'

interface Props {
  open: boolean
  onClose: () => void
}

/**
 * Minimal "new space" form. Preset is always ``collaborative`` (the only
 * preset a user can create today); ``personal`` spaces are provisioned by
 * onboarding, ``managed`` spaces will land with a later slice.
 */
export function CreateSpaceDialog({ open, onClose }: Props) {
  const { t } = useTranslation()
  const { createNewSpace } = useSpaces()
  const { credentials } = useAuth()
  // Personal-tier users have a single-member org; org concepts stay hidden.
  const isPersonal = credentials?.tier === 'personal'
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [description, setDescription] = useState('')
  const [visibility, setVisibility] = useState<CreateSpaceInput['visibility']>('private')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) {
      setName('')
      setSlug('')
      setDescription('')
      setVisibility('private')
      setError(null)
      setSubmitting(false)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [open, submitting, onClose])

  if (!open) return null

  const canSubmit = name.trim().length > 0 && slug.trim().length > 0 && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await createNewSpace({
        name: name.trim(),
        slug: slug.trim().toLowerCase(),
        description: description.trim() || undefined,
        visibility,
      })
      onClose()
    } catch (err) {
      setError(err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card">
        <header className="dialog-header">
          <h2>{t(isPersonal ? 'createSpace.titlePersonal' : 'createSpace.title')}</h2>
          <button
            aria-label={t('createSpace.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form onSubmit={handleSubmit} className="dialog-form">
          <label>
            <span>{t('createSpace.name')}</span>
            <input
              autoFocus
              onChange={(e) => setName(e.target.value)}
              placeholder={t(
                isPersonal
                  ? 'createSpace.namePlaceholderPersonal'
                  : 'createSpace.namePlaceholder',
              )}
              type="text"
              value={name}
            />
          </label>
          <label>
            <span>{t('createSpace.slug')}</span>
            <input
              onChange={(e) => setSlug(e.target.value)}
              placeholder="rocket"
              type="text"
              value={slug}
            />
          </label>
          <label>
            <span>{t('createSpace.description')}</span>
            <textarea
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('createSpace.descriptionPlaceholder')}
              rows={3}
              value={description}
            />
          </label>
          {/* Visibility is an org concept — meaningless for a single-member
              personal org, so we hide it and keep the default 'private'. */}
          {!isPersonal && (
            <label>
              <span>{t('createSpace.visibility')}</span>
              <select
                onChange={(e) => setVisibility(e.target.value as CreateSpaceInput['visibility'])}
                value={visibility}
              >
                <option value="private">{t('spaces.visibility.private')}</option>
                <option value="organization">{t('spaces.visibility.organization')}</option>
              </select>
            </label>
          )}
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('createSpace.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('createSpace.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
