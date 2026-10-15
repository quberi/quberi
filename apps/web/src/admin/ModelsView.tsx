import { useEffect, useState } from 'react'
import { Cpu, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-react'

import {
  AdminApiError,
  createPlatformBinding,
  deletePlatformBinding,
  listPlatformBindings,
  updatePlatformBinding,
  type ModelCatalogEntry,
  type PlatformBindingDetail,
} from './api'

// Known base URLs per vendor — used only to pre-fill the editor (and to
// show a vendor-accurate placeholder instead of a misleading hard-coded
// example) when an admin registers a new platform key. Purely a UX
// convenience; any base_url can still be typed/edited freely.
const VENDOR_BASE_URLS: Record<string, string> = {
  deepseek: 'https://api.deepseek.com',
  openai: 'https://api.openai.com/v1',
  dashscope: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  moonshot: 'https://api.moonshot.cn/v1',
  zhipu: 'https://open.bigmodel.cn/api/paas/v4',
}

/**
 * Platform-shared model management — what used to require editing
 * ``.env`` and restarting the API (one platform key per provider_kind)
 * is now a page: register a key for any vendor, give it a priority
 * among other keys for the same vendor (lower tried first), pause one
 * without deleting it, or rotate/remove it outright.
 */
export function ModelsView({
  token,
  modelCatalog,
}: {
  token: string
  modelCatalog: ModelCatalogEntry[]
}) {
  const [bindings, setBindings] = useState<PlatformBindingDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [editing, setEditing] = useState<PlatformBindingDetail | 'new' | null>(null)

  const load = async (isRefresh: boolean) => {
    isRefresh ? setRefreshing(true) : setLoading(true)
    setError(null)
    try {
      const res = await listPlatformBindings(token)
      setBindings(res.items)
    } catch (err) {
      setError(err instanceof AdminApiError ? err.message : String(err))
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => {
    void load(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Group by vendor (falling back to provider_kind for vendor-less rows
  // like the local embedding binding), each group sorted by priority so
  // the one the resolver would actually pick shows first.
  const groups = new Map<string, PlatformBindingDetail[]>()
  for (const b of bindings) {
    const key = b.vendor ?? `__${b.provider_kind}`
    const list = groups.get(key) ?? []
    list.push(b)
    groups.set(key, list)
  }
  for (const list of groups.values()) {
    list.sort((a, b) => a.priority - b.priority)
  }

  const chatModels = modelCatalog.filter((m) => m.purpose === 'chat')
  const embeddingModels = modelCatalog.filter((m) => m.purpose === 'embedding')

  return (
    <section className="admin-section">
      <div className="admin-section-head">
        <h2>
          <Cpu size={16} /> Platform-shared models
        </h2>
        <div className="admin-section-actions">
          <button disabled={refreshing} onClick={() => void load(true)} type="button">
            <RefreshCw size={14} className={refreshing ? 'spin' : undefined} /> Refresh
          </button>
          <button className="admin-button-primary" onClick={() => setEditing('new')} type="button">
            <Plus size={14} /> Add key
          </button>
        </div>
      </div>
      <p className="admin-section-hint">
        Keys shown here are available to every organisation (subject to each
        org's own platform-model visibility setting). Several keys may
        share a vendor — the active one with the lowest priority number is
        used; pause a key to take it out of rotation without deleting it.
      </p>

      {loading && <div className="admin-state">Loading…</div>}
      {error && !loading && (
        <div className="admin-state admin-state-error">{error}</div>
      )}

      {!loading && !error && (
        <div className="admin-vendor-groups">
          {[...groups.entries()].map(([key, list]) => (
            <div className="admin-vendor-group" key={key}>
              <h3>{list[0].vendor_display_name || list[0].provider_kind}</h3>
              <div className="admin-table-wrap">
                <table className="admin-table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Base URL</th>
                      <th>Priority</th>
                      <th>Status</th>
                      <th>Models</th>
                      <th aria-label="actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((b) => (
                      <tr key={b.id}>
                        <td>
                          <strong>{b.display_name}</strong>
                          <small>{b.api_key_present ? 'Key configured' : 'No key'}</small>
                        </td>
                        <td>{b.base_url ?? '—'}</td>
                        <td>{b.priority}</td>
                        <td>
                          <span className={`admin-chip admin-chip-status-${b.status}`}>
                            {b.status}
                          </span>
                        </td>
                        <td>{b.models.length > 0 ? b.models.join(', ') : '—'}</td>
                        <td className="admin-table-row-actions">
                          <button onClick={() => setEditing(b)} type="button" title="Edit">
                            <Pencil size={13} />
                          </button>
                          <button
                            onClick={() => void handleDelete(b)}
                            type="button"
                            title="Delete"
                            className="admin-danger"
                          >
                            <Trash2 size={13} />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
          {bindings.length === 0 && (
            <div className="admin-state">No platform-default bindings configured.</div>
          )}
        </div>
      )}

      {editing && (
        <BindingEditorDialog
          token={token}
          binding={editing === 'new' ? null : editing}
          chatModels={chatModels}
          embeddingModels={embeddingModels}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            void load(true)
          }}
        />
      )}
    </section>
  )

  async function handleDelete(b: PlatformBindingDetail) {
    if (!window.confirm(`Delete platform key "${b.display_name}"? This cannot be undone.`)) {
      return
    }
    try {
      await deletePlatformBinding(token, b.id)
      void load(true)
    } catch (err) {
      window.alert(err instanceof AdminApiError ? err.message : String(err))
    }
  }
}

function BindingEditorDialog({
  token,
  binding,
  chatModels,
  embeddingModels,
  onClose,
  onSaved,
}: {
  token: string
  binding: PlatformBindingDetail | null
  chatModels: ModelCatalogEntry[]
  embeddingModels: ModelCatalogEntry[]
  onClose: () => void
  onSaved: () => void
}) {
  const isNew = binding === null
  const [vendor, setVendor] = useState(binding?.vendor ?? '')
  const [displayName, setDisplayName] = useState(binding?.display_name ?? '')
  const [baseUrl, setBaseUrl] = useState(binding?.base_url ?? '')
  // Tracks whether the admin has manually edited Base URL, so the vendor
  // auto-fill (above) only kicks in while the field is still blank/default.
  const [baseUrlTouched, setBaseUrlTouched] = useState(binding !== null)
  const [apiKey, setApiKey] = useState('')
  const [priority, setPriority] = useState(binding?.priority ?? 100)
  const [status, setStatus] = useState(binding?.status ?? 'active')
  const [selectedModels, setSelectedModels] = useState<Set<string>>(
    new Set(binding?.models ?? []),
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const toggleModel = (modelId: string) => {
    const next = new Set(selectedModels)
    if (next.has(modelId)) next.delete(modelId)
    else next.add(modelId)
    setSelectedModels(next)
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      if (isNew) {
        await createPlatformBinding(token, {
          provider_kind: 'openai_compat',
          vendor: vendor.trim() || null,
          display_name: displayName.trim(),
          base_url: baseUrl.trim() || null,
          api_key: apiKey.trim() || null,
          priority,
          models: [...selectedModels],
        })
      } else {
        await updatePlatformBinding(token, binding.id, {
          display_name: displayName.trim(),
          base_url: baseUrl.trim() || null,
          api_key: apiKey.trim() || undefined,
          status,
          priority,
          models: [...selectedModels],
        })
      }
      onSaved()
    } catch (err) {
      setError(err instanceof AdminApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
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
          <strong>{isNew ? 'Add platform key' : `Edit ${binding.display_name}`}</strong>
          <button onClick={onClose} aria-label="Close" type="button">
            <X size={16} />
          </button>
        </header>
        <form className="admin-drawer-body admin-form" onSubmit={submit}>
          {isNew && (
            <label className="admin-field">
              <span>Vendor</span>
              <input
                value={vendor}
                onChange={(e) => {
                  const v = e.target.value
                  setVendor(v)
                  // Auto-fill the base URL the first time a known vendor is
                  // typed, but only while the field is still untouched —
                  // never clobber a value the admin already edited.
                  const known = VENDOR_BASE_URLS[v.trim().toLowerCase()]
                  if (known && !baseUrlTouched) setBaseUrl(known)
                }}
                placeholder="e.g. deepseek, openai, dashscope"
                required
              />
            </label>
          )}
          <label className="admin-field">
            <span>Display name</span>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
            />
          </label>
          <label className="admin-field">
            <span>Base URL</span>
            <input
              value={baseUrl}
              onChange={(e) => {
                setBaseUrl(e.target.value)
                setBaseUrlTouched(true)
              }}
              placeholder={
                VENDOR_BASE_URLS[vendor.trim().toLowerCase()] ?? 'https://api.example.com/v1'
              }
            />
          </label>
          <label className="admin-field">
            <span>{isNew ? 'API key' : 'API key (leave blank to keep current)'}</span>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
            />
          </label>
          <label className="admin-field">
            <span>Priority (lower tried first)</span>
            <input
              type="number"
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value) || 100)}
            />
          </label>
          {!isNew && (
            <label className="admin-field">
              <span>Status</span>
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="active">active</option>
                <option value="paused">paused</option>
                <option value="error">error</option>
                <option value="archived">archived</option>
              </select>
            </label>
          )}
          <div className="admin-field">
            <span>
              {binding?.provider_kind === 'local_embedding' 
                ? 'Embedding models this key serves' 
                : 'Chat models this key serves'}
            </span>
            <div className="admin-model-allowlist">
              {(binding?.provider_kind === 'local_embedding' 
                ? embeddingModels 
                : chatModels
              ).map((m) => (
                <label className="admin-checkbox-option" key={m.model_id}>
                  <input
                    type="checkbox"
                    checked={selectedModels.has(m.model_id)}
                    onChange={() => toggleModel(m.model_id)}
                  />
                  <span>
                    {m.display_name}
                    <small>{m.model_id}</small>
                  </span>
                </label>
              ))}
            </div>
          </div>

          {error && <div className="admin-state admin-state-error">{error}</div>}

          <div className="admin-form-actions">
            <button type="button" onClick={onClose} disabled={saving}>
              Cancel
            </button>
            <button type="submit" className="admin-button-primary" disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </form>
      </aside>
    </div>
  )
}
