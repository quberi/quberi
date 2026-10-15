import { Archive, KeyRound, Loader2, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { ROLE_CODES, SUBJECT_TYPES, SpaceAccessPanel } from '../spaceAccess/SpaceAccessPanel'
import type { AccessSubjectType, SpaceRoleCode } from '../spaceAccess/types'
import { subjectIdForApi, useSubjectCandidates } from '../spaceAccess/useSubjectCandidates'

import { archiveSpace, createSpace, listOrganizationSpaces } from './api'
import { useSpaces } from './SpacesContext'
import type { SpaceSummary } from './types'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: SpaceSummary[] }

function toMessage(error: unknown): string {
  return error instanceof ApiError ? error.detail || `HTTP ${error.status}` : String(error)
}

/**
 * Admin-console page for enterprise-owned spaces.
 *
 * An enterprise space belongs to the organisation rather than to whoever
 * created it, and is shared by *structure*: the whole organisation, units
 * (with their sub-units), positions, groups or individual members, each
 * with its own role. Owners/admins can create them, change who has access
 * and archive them even when they are not a member themselves — which is
 * administration only; it does not expose the space's contents.
 */
export function OrgSpacesPanel() {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const { reload: reloadWorkspaceSpaces } = useSpaces()
  const [state, setState] = useState<LoadState>({ kind: 'loading' })
  const [createOpen, setCreateOpen] = useState(false)
  const [managingId, setManagingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!credentials) return
    setState({ kind: 'loading' })
    try {
      const { items } = await listOrganizationSpaces(credentials)
      setState({ kind: 'ready', items })
    } catch (err) {
      setState({ kind: 'error', message: toMessage(err) })
    }
  }, [credentials])

  useEffect(() => {
    void load()
  }, [load])

  const doArchive = async (space: SpaceSummary) => {
    if (!credentials) return
    if (!window.confirm(t('orgSpaces.confirmArchive', { name: space.name }))) return
    try {
      await archiveSpace(credentials, space.id)
      if (managingId === space.id) setManagingId(null)
      await Promise.all([load(), reloadWorkspaceSpaces()])
    } catch (err) {
      window.alert(toMessage(err))
    }
  }

  return (
    <>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="section-label">{t('orgSpaces.title')}</span>
            <p>{t('orgSpaces.description')}</p>
          </div>
          <div className="panel-actions">
            <button
              className="secondary-button icon-button"
              disabled={state.kind === 'loading'}
              onClick={() => void load()}
              title={t('orgSpaces.refresh')}
              type="button"
            >
              <RefreshCw size={13} className={state.kind === 'loading' ? 'spin' : undefined} />
            </button>
            <button className="primary-button" onClick={() => setCreateOpen(true)} type="button">
              <Plus size={14} />
              <span>{t('orgSpaces.create')}</span>
            </button>
          </div>
        </div>

        {state.kind === 'loading' && <div className="state-panel">{t('orgSpaces.loading')}</div>}
        {state.kind === 'error' && (
          <div className="state-panel error-state">
            {t('orgSpaces.loadError')}: {state.message}
          </div>
        )}
        {state.kind === 'ready' && state.items.length === 0 && (
          <div className="state-panel">{t('orgSpaces.empty')}</div>
        )}
        {state.kind === 'ready' && state.items.length > 0 && (
          <ul className="space-access-list">
            {state.items.map((space) => {
              const archived = space.status === 'archived'
              const managing = managingId === space.id
              return (
                <li className="space-access-row" key={space.id}>
                  <span className="space-access-subject">
                    <span>
                      <strong>{space.name}</strong>
                      <small>{space.slug}</small>
                    </span>
                  </span>
                  <span className={archived ? 'badge' : 'badge role-viewer'}>
                    {t(`spaces.status.${space.status}`)}
                  </span>
                  {!archived && (
                    <button
                      className={managing ? 'primary-button' : 'secondary-button'}
                      onClick={() => setManagingId(managing ? null : space.id)}
                      type="button"
                    >
                      <KeyRound size={13} />
                      <span>{t(managing ? 'orgSpaces.hideAccess' : 'orgSpaces.manageAccess')}</span>
                    </button>
                  )}
                  {!archived && (
                    <button
                      className="icon-button danger"
                      onClick={() => void doArchive(space)}
                      title={t('orgSpaces.archive')}
                      type="button"
                    >
                      <Archive size={13} />
                    </button>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>

      {managingId && <SpaceAccessPanel spaceId={managingId} />}

      <CreateOrgSpaceDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={async (space) => {
          setCreateOpen(false)
          setManagingId(space.id)
          await Promise.all([load(), reloadWorkspaceSpaces()])
        }}
      />
    </>
  )
}

/* ========================================================================
 * Create dialog
 * ==================================================================== */

interface GrantDraft {
  key: number
  subjectType: AccessSubjectType
  subjectId: string
  role: SpaceRoleCode
}

function CreateOrgSpaceDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  onCreated: (space: SpaceSummary) => Promise<void>
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [description, setDescription] = useState('')
  const [grants, setGrants] = useState<GrantDraft[]>([])
  const [nextKey, setNextKey] = useState(1)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) return
    setName('')
    setSlug('')
    setDescription('')
    setGrants([])
    setError(null)
    setSubmitting(false)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [open, submitting, onClose])

  if (!open) return null

  const addGrant = () => {
    setGrants((prev) => [
      ...prev,
      { key: nextKey, subjectType: 'organization_unit', subjectId: '', role: 'viewer' },
    ])
    setNextKey((k) => k + 1)
  }
  const patchGrant = (key: number, patch: Partial<GrantDraft>) =>
    setGrants((prev) => prev.map((g) => (g.key === key ? { ...g, ...patch } : g)))
  const removeGrant = (key: number) => setGrants((prev) => prev.filter((g) => g.key !== key))

  const grantsComplete = grants.every((g) => g.subjectId.length > 0)
  const canSubmit =
    name.trim().length > 0 && slug.trim().length > 0 && grantsComplete && !submitting

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit || !credentials) return
    setSubmitting(true)
    setError(null)
    try {
      const detail = await createSpace(credentials, {
        name: name.trim(),
        slug: slug.trim().toLowerCase(),
        description: description.trim() || undefined,
        visibility: 'organization',
        owner: 'organization',
        grants: grants.map((g) => ({
          subject_type: g.subjectType,
          subject_id: subjectIdForApi(g.subjectType, g.subjectId),
          role: g.role,
        })),
      })
      await onCreated(detail)
    } catch (err) {
      setError(toMessage(err))
      setSubmitting(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card org-space-dialog">
        <header className="dialog-header">
          <h2>{t('orgSpaces.dialog.title')}</h2>
          <button
            aria-label={t('orgSpaces.dialog.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form className="dialog-form" onSubmit={handleSubmit}>
          <label>
            <span>{t('orgSpaces.dialog.name')}</span>
            <input
              autoFocus
              onChange={(e) => setName(e.target.value)}
              placeholder={t('orgSpaces.dialog.namePlaceholder')}
              type="text"
              value={name}
            />
          </label>
          <label>
            <span>{t('orgSpaces.dialog.slug')}</span>
            <input
              onChange={(e) => setSlug(e.target.value)}
              placeholder="engineering-handbook"
              type="text"
              value={slug}
            />
          </label>
          <label>
            <span>{t('orgSpaces.dialog.description')}</span>
            <textarea
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('orgSpaces.dialog.descriptionPlaceholder')}
              rows={2}
              value={description}
            />
          </label>

          <div className="grant-editor">
            <div className="grant-editor-head">
              <span>{t('orgSpaces.dialog.access')}</span>
              <button className="secondary-button" onClick={addGrant} type="button">
                <Plus size={13} />
                <span>{t('orgSpaces.dialog.addGrant')}</span>
              </button>
            </div>
            <p className="dialog-hint">{t('orgSpaces.dialog.accessHint')}</p>
            {grants.length === 0 && (
              <div className="state-panel">{t('orgSpaces.dialog.noGrants')}</div>
            )}
            {grants.map((g) => (
              <GrantRow
                grant={g}
                key={g.key}
                onChange={(patch) => patchGrant(g.key, patch)}
                onRemove={() => removeGrant(g.key)}
              />
            ))}
          </div>

          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('orgSpaces.dialog.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('orgSpaces.dialog.submit')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function GrantRow({
  grant,
  onChange,
  onRemove,
}: {
  grant: GrantDraft
  onChange: (patch: Partial<GrantDraft>) => void
  onRemove: () => void
}) {
  const { t } = useTranslation()
  const candidates = useSubjectCandidates(grant.subjectType)

  // Keep the picked subject valid for the current type: default to the first
  // candidate, and re-pick if the type changed and the old id no longer applies.
  useEffect(() => {
    if (!candidates.some((c) => c.id === grant.subjectId)) {
      onChange({ subjectId: candidates[0]?.id ?? '' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidates, grant.subjectId])

  return (
    <div className="grant-row">
      <select
        aria-label={t('spaceAccess.subjectType')}
        onChange={(e) =>
          onChange({ subjectType: e.target.value as AccessSubjectType, subjectId: '' })
        }
        value={grant.subjectType}
      >
        {SUBJECT_TYPES.map((s) => (
          <option key={s} value={s}>
            {t(`spaceAccess.subjectTypeOption.${s}`)}
          </option>
        ))}
      </select>
      <select
        aria-label={t('spaceAccess.subject')}
        onChange={(e) => onChange({ subjectId: e.target.value })}
        value={grant.subjectId}
      >
        {candidates.length === 0 && (
          <option value="">{t('spaceAccess.addDialog.noCandidates')}</option>
        )}
        {candidates.map((c) => (
          <option key={c.id} value={c.id}>
            {c.label}
            {c.subtitle ? ` · ${c.subtitle}` : ''}
          </option>
        ))}
      </select>
      <select
        aria-label={t('spaceAccess.role')}
        onChange={(e) => onChange({ role: e.target.value as SpaceRoleCode })}
        value={grant.role}
      >
        {ROLE_CODES.map((r) => (
          <option key={r} value={r}>
            {t(`members.role.${r}`)}
          </option>
        ))}
      </select>
      <button
        aria-label={t('orgSpaces.dialog.removeGrant')}
        className="icon-button danger"
        onClick={onRemove}
        type="button"
      >
        <Trash2 size={13} />
      </button>
    </div>
  )
}
