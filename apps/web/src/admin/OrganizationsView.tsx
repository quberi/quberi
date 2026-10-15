import { useEffect, useState } from 'react'
import { Users, X } from 'lucide-react'

import {
  AdminApiError,
  getOrgPlatformModelVisibility,
  setOrgPlatformModelVisibility,
  type ModelCatalogEntry,
  type OrganizationStatsRow,
  type PlatformModelVisibility,
} from './api'

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
  const [selected, setSelected] = useState<OrganizationStatsRow | null>(null)

  return (
    <section className="admin-section">
      <h2>
        <Users size={16} /> Organizations ({orgs.length})
      </h2>
      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Tier</th>
              <th>Owner</th>
              <th>Members</th>
              <th>Spaces</th>
              <th>Docs</th>
              <th>Conversations</th>
              <th>Messages</th>
              <th>Created</th>
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
                  {o.is_demo && <span className="admin-chip">demo</span>}
                </td>
                <td>{o.tier}</td>
                <td>{o.owner_email ?? '—'}</td>
                <td>{o.member_count}</td>
                <td>{o.space_count}</td>
                <td>{o.document_count}</td>
                <td>{o.conversation_count}</td>
                <td>{o.message_count}</td>
                <td>{new Date(o.created_at).toLocaleString()}</td>
              </tr>
            ))}
            {orgs.length === 0 && (
              <tr>
                <td colSpan={9} className="admin-table-empty">
                  No organizations yet.
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
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'error'; message: string }
    | { kind: 'ready'; mode: VisibilityMode; modelIds: Set<string> }
  >({ kind: 'loading' })
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getOrgPlatformModelVisibility(token, org.id)
      .then((v) => {
        if (cancelled) return
        setState({ kind: 'ready', mode: v.mode, modelIds: new Set(v.model_ids) })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setState({
          kind: 'error',
          message: err instanceof AdminApiError ? err.message : String(err),
        })
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
      setSaveError(err instanceof AdminApiError ? err.message : String(err))
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
          <button onClick={onClose} aria-label="Close" type="button">
            <X size={16} />
          </button>
        </header>

        <div className="admin-drawer-body">
          <h3>Platform model visibility</h3>
          <p className="admin-section-hint">
            Controls whether this org's members see Quberi's platform-shared
            chat models (DeepSeek, OpenAI, ...) in their own model picker.
            Never affects the org's own BYOK models — those stay visible
            regardless of this setting.
          </p>

          {state.kind === 'loading' && <div className="admin-state">Loading…</div>}
          {state.kind === 'error' && (
            <div className="admin-state admin-state-error">{state.message}</div>
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
                    <strong>All</strong>
                    <small>Every platform model is visible (default).</small>
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
                    <strong>None</strong>
                    <small>Hide every platform model — only this org's own BYOK models show.</small>
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
                    <strong>Allowlist</strong>
                    <small>Only the models checked below are visible.</small>
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
                    <div className="admin-section-hint">No chat models in the registry.</div>
                  )}
                </div>
              )}

              {saveError && <div className="admin-state admin-state-error">{saveError}</div>}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}
