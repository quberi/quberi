import { Loader2, UserPlus, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'

import { useMembers } from './MembersContext'
import type { InvitableRole } from './types'

interface Props {
  open: boolean
  onClose: () => void
}

// ``admin`` and ``member`` are the only *organisation-level* roles the API
// accepts on invite today. Editor / viewer are space-level and get granted
// in the (future) Space members panel instead.
const INVITABLE_ROLES: InvitableRole[] = ['admin', 'member']

export function InviteMemberDialog({ open, onClose }: Props) {
  const { t } = useTranslation()
  const { invite } = useMembers()
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [role, setRole] = useState<InvitableRole>('member')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) {
      setEmail('')
      setDisplayName('')
      setRole('member')
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

  const canSubmit = email.trim().length > 0 && displayName.trim().length > 0 && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await invite({
        email: email.trim().toLowerCase(),
        display_name: displayName.trim(),
        role,
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
          <h2>{t('inviteMember.title')}</h2>
          <button
            aria-label={t('inviteMember.cancel')}
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
            <span>{t('inviteMember.email')}</span>
            <input
              autoFocus
              onChange={(e) => setEmail(e.target.value)}
              placeholder="teammate@acme.example"
              type="email"
              value={email}
            />
          </label>
          <label>
            <span>{t('inviteMember.displayName')}</span>
            <input
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder={t('inviteMember.displayNamePlaceholder')}
              type="text"
              value={displayName}
            />
          </label>
          <label>
            <span>{t('inviteMember.role')}</span>
            <select onChange={(e) => setRole(e.target.value as InvitableRole)} value={role}>
              {INVITABLE_ROLES.map((r) => (
                <option key={r} value={r}>
                  {t(`members.role.${r}`)}
                </option>
              ))}
            </select>
          </label>
          <p className="dialog-hint">{t('inviteMember.hint')}</p>
          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('inviteMember.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : <UserPlus size={16} />}
              <span>{t('inviteMember.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
