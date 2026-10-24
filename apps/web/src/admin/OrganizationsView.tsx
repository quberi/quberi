import { useEffect, useState } from 'react'
import { Users, X } from 'lucide-react'

import {
  getOrgPlatformModelVisibility,
  setOrgPlatformModelVisibility,
  type ModelCatalogEntry,
  type OrganizationStatsRow,
  type PlatformModelVisibility,
} from './api'
import { useLang } from './i18n'
import { errorMessage } from './i18n/errors'

/**
 * Organisations table + a detail panel for the platform-admin-only
 * governance control: which platform-shared chat models (if any) a given
 * org's members may see in their own model picker. This sits above the
 * org's own per-member ``model_access_policies`` — a model must clear
 * this org-level gate before per-member policy is even evaluated.
 */
export function OrganizationsView({
  orgs,
  token,
  modelCatalog,
}: {
  orgs: OrganizationStatsRow[]
  token: string
  modelCatalog: ModelCatalogEntry[]
}) {
  const { t, formatDateTime } = useLang()
  const [selected, setSelected] = useState<OrganizationStatsRow | null>(null)

  return (
    <section className="admin-section">
      <h2>
        <Users size={16} /> {t('orgs.title', { n: orgs.length })}
      </h2>
      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>{t('orgs.col.name')}</th>
              <th>{t('orgs.col.tier')}</th>
              <th>{t('orgs.col.owner')}</th>
              <th>{t('orgs.col.members')}</th>
              <th>{t('orgs.col.spaces')}</th>
              <th>{t('orgs.col.docs')}</th>
              <th>{t('orgs.col.conversations')}</th>
              <th>{t('orgs.col.messages')}</th>
              <th>{t('orgs.col.created')}</th>
            </tr>
          </thead>
          <tbody>
            {orgs.map((o) => (
              <tr
                key={o.id}
                className={o.is_demo ? 'is-demo admin-row-clickable' : 'admin-row-clickable'}
                onClick={() => setSelected(o)}
              >
                <td>
                  <strong>{o.name}</strong>
                  <small>{o.slug}</small>
                  {o.is_demo && <span className="admin-chip">{t('orgs.demo')}</span>}
                </td>
                <td>{o.tier === 'personal' || o.tier === 'team' ? t(`orgs.tier.${o.tier}`) : o.tier}</td>
                <td>{o.owner_email ?? '—'}</td>
                <td>{o.member_count}</td>
                <td>{o.space_count}</td>
                <td>{o.document_count}</td>
                <td>{o.conversation_count}</td>
                <td>{o.message_count}</td>
                <td>{formatDateTime(o.created_at)}</td>
              </tr>
            ))}
            {orgs.length === 0 && (
              <tr>
                <td colSpan={9} className="admin-table-empty">
                  {t('orgs.empty')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && (
        <OrgDetailDrawer
          org={selected}
          token={token}
          modelCatalog={modelCatalog}
          onClose={() => setSelected(null)}
        />
      )}
    </section>
  )
}

type VisibilityMode = PlatformModelVisibility['mode']

function OrgDetailDrawer({
  org,
  token,
  modelCatalog,
  onClose,
}: {
  org: OrganizationStatsRow
  token: string
  modelCatalog: ModelCatalogEntry[]
  onClose: () => void
}) {
  const { t, tCode } = useLang()
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'error'; err: unknown }
    | { kind: 'ready'; mode: VisibilityMode; modelIds: Set<string> }
  >({ kind: 'loading' })
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<{ err: unknown } | null>(null)

  useEffect(() => {
    let cancelled = false
    getOrgPlatformModelVisibility(token, org.id)
      .then((v) => {
        if (cancelled) return
        setState({ kind: 'ready', mode: v.mode, modelIds: new Set(v.model_ids) })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setState({ kind: 'error', err })
      })
    return () => {
      cancelled = true
    }
  }, [token, org.id])

  const chatModels = modelCatalog.filter((m) => m.purpose === 'chat')

  const save = async (mode: VisibilityMode, modelIds: Set<string>) => {
    setSaving(true)
    setSaveError(null)
    try {
      await setOrgPlatformModelVisibility(token, org.id, {
        mode,
        model_ids: mode === 'allowlist' ? [...modelIds] : undefined,
      })
      setState({ kind: 'ready', mode, modelIds })
    } catch (err) {
      setSaveError({ err })
    } finally {
      setSaving(false)
    }
  }

  const setMode = (mode: VisibilityMode) => {
    if (state.kind !== 'ready') return
    void save(mode, state.modelIds)
  }

  const toggleModel = (modelId: string) => {
    if (state.kind !== 'ready') return
    const next = new Set(state.modelIds)
    if (next.has(modelId)) next.delete(modelId)
    else next.add(modelId)
    void save('allowlist', next)
  }

  return (
    <div className="admin-drawer-backdrop" onClick={onClose} role="presentation">
      <aside
        className="admin-drawer"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="admin-drawer-head">
          <div>
            <strong>{org.name}</strong>
            <small>{org.slug}</small>
          </div>
          <button onClick={onClose} aria-label={t('common.close')} type="button">
            <X size={16} />
          </button>
        </header>

        <div className="admin-drawer-body">
          <h3>{t('orgs.drawer.title')}</h3>
          <p className="admin-section-hint">
            {t('orgs.drawer.hint')}
          </p>

          {state.kind === 'loading' && <div className="admin-state">{t('common.loading')}</div>}
          {state.kind === 'error' && (
            <div className="admin-state admin-state-error">{errorMessage(state.err, tCode, t)}</div>
          )}

          {state.kind === 'ready' && (
            <>
              <div className="admin-radio-group">
                <label className="admin-radio-option">
                  <input
                    type="radio"
                    name="visibility-mode"
                    checked={state.mode === 'all'}
                    disabled={saving}
                    onChange={() => setMode('all')}
                  />
                  <span>
                    <strong>{t('orgs.mode.all')}</strong>
                    <small>{t('orgs.mode.allHint')}</small>
                  </span>
                </label>
                <label className="admin-radio-option">
                  <input
                    type="radio"
                    name="visibility-mode"
                    checked={state.mode === 'none'}
                    disabled={saving}
                    onChange={() => setMode('none')}
                  />
                  <span>
                    <strong>{t('orgs.mode.none')}</strong>
                    <small>{t('orgs.mode.noneHint')}</small>
                  </span>
                </label>
                <label className="admin-radio-option">
                  <input
                    type="radio"
                    name="visibility-mode"
                    checked={state.mode === 'allowlist'}
                    disabled={saving}
                    onChange={() => setMode('allowlist')}
                  />
                  <span>
                    <strong>{t('orgs.mode.allowlist')}</strong>
                    <small>{t('orgs.mode.allowlistHint')}</small>
                  </span>
                </label>
              </div>

              {state.mode === 'allowlist' && (
                <div className="admin-model-allowlist">
                  {chatModels.map((m) => (
                    <label className="admin-checkbox-option" key={m.model_id}>
                      <input
                        type="checkbox"
                        checked={state.modelIds.has(m.model_id)}
                        disabled={saving}
                        onChange={() => toggleModel(m.model_id)}
                      />
                      <span>
                        {m.display_name}
                        <small>{m.vendor_display_name}</small>
                      </span>
                    </label>
                  ))}
                  {chatModels.length === 0 && (
                    <div className="admin-section-hint">{t('orgs.noChatModels')}</div>
                  )}
                </div>
              )}

              {saveError && <div className="admin-state admin-state-error">{errorMessage(saveError.err, tCode, t)}</div>}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}
