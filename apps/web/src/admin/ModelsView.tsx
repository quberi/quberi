import { useEffect, useState } from 'react'
import { Cpu, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-react'

import {
  createPlatformBinding,
  deletePlatformBinding,
  listPlatformBindings,
  updatePlatformBinding,
  type ModelCatalogEntry,
  type PlatformBindingDetail,
} from './api'
import { useLang, type I18n } from './i18n'
import { errorMessage } from './i18n/errors'
import { T } from './i18n/Rich'

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

const STATUSES = ['active', 'paused', 'error', 'archived'] as const

/** A key's status in the active language; a status this console does not know shows as sent. */
function statusLabel(t: I18n['t'], status: string): string {
  const known = STATUSES.find((s) => s === status)
  return known ? t(`models.status.${known}`) : status
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
  const { t, tCode } = useLang()
  const [bindings, setBindings] = useState<PlatformBindingDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<{ err: unknown } | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [editing, setEditing] = useState<PlatformBindingDetail | 'new' | null>(null)
  // "Text models" is the page as it always was. "Vision models" shows the same keys, but only
  // the models on them that read pictures, so an administrator can see which keys can serve a
  // vision model and add one (an organisation then chooses it in its own model settings).
  const [tab, setTab] = useState<'text' | 'vision'>('text')

  const load = async (isRefresh: boolean) => {
    isRefresh ? setRefreshing(true) : setLoading(true)
    setError(null)
    try {
      const res = await listPlatformBindings(token)
      setBindings(res.items)
    } catch (err) {
      setError({ err })
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
  const visionModels = modelCatalog.filter((m) => m.supports_vision === true)
  const visionIds = new Set(visionModels.map((m) => m.model_id))
  // Only a key that speaks the OpenAI-style protocol can carry a vision model (a local
  // embedding key cannot).
  const shownGroups = [...groups.entries()]
    .map(([key, list]): [string, PlatformBindingDetail[]] => [
      key,
      tab === 'vision' ? list.filter((b) => b.provider_kind === 'openai_compat') : list,
    ])
    .filter(([, list]) => list.length > 0)

  return (
    <section className="admin-section">
      <div className="admin-section-head">
        <h2>
          <Cpu size={16} /> {t('models.title')}
        </h2>
        <div className="admin-section-actions">
          <button disabled={refreshing} onClick={() => void load(true)} type="button">
            <RefreshCw size={14} className={refreshing ? 'spin' : undefined} />{' '}
            {t('common.refresh')}
          </button>
          <button className="admin-button-primary" onClick={() => setEditing('new')} type="button">
            <Plus size={14} /> {t('models.addKey')}
          </button>
        </div>
      </div>
      <p className="admin-section-hint">
        {t('models.hint')}
      </p>

      <div className="admin-tabs" role="tablist">
        {(['text', 'vision'] as const).map((id) => (
          <button
            aria-selected={tab === id}
            className={tab === id ? 'active' : ''}
            key={id}
            onClick={() => setTab(id)}
            role="tab"
            type="button"
          >
            {id === 'text' ? t('models.tab.text') : t('models.tab.vision')}
          </button>
        ))}
      </div>
      {tab === 'vision' && (
        <p className="admin-section-hint">
          <T k="models.visionHint" />
        </p>
      )}
      {loading && <div className="admin-state">{t('common.loading')}</div>}
      {error && !loading && (
        <div className="admin-state admin-state-error">
          {errorMessage(error.err, tCode, t)}
        </div>
      )}

      {!loading && !error && (
        <div className="admin-vendor-groups">
          {shownGroups.map(([key, list]) => (
            <div className="admin-vendor-group" key={key}>
              <h3>{list[0].vendor_display_name || list[0].provider_kind}</h3>
              <div className="admin-table-wrap">
                <table className="admin-table">
                  <thead>
                    <tr>
                      <th>{t('models.col.name')}</th>
                      <th>{t('models.col.baseUrl')}</th>
                      <th>{t('models.col.priority')}</th>
                      <th>{t('models.col.status')}</th>
                      <th>{t('models.col.models')}</th>
                      <th aria-label={t('models.col.actions')} />
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((b) => (
                      <tr key={b.id}>
                        <td>
                          <strong>{b.display_name}</strong>
                          <small>{b.api_key_present ? t('models.keyConfigured') : t('models.noKey')}</small>
                        </td>
                        <td>{b.base_url ?? '—'}</td>
                        <td>{b.priority}</td>
                        <td>
                          <span className={`admin-chip admin-chip-status-${b.status}`}>
                            {statusLabel(t, b.status)}
                          </span>
                        </td>
                        <td>{shownModels(b.models, tab, visionIds)}</td>
                        <td className="admin-table-row-actions">
                          <button onClick={() => setEditing(b)} type="button" title={t('common.edit')}>
                            <Pencil size={13} />
                          </button>
                          <button
                            onClick={() => void handleDelete(b)}
                            type="button"
                            title={t('common.delete')}
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
            <div className="admin-state">{t('models.empty')}</div>
          )}
        </div>
      )}

      {editing && (
        <BindingEditorDialog
          token={token}
          binding={editing === 'new' ? null : editing}
          chatModels={chatModels}
          embeddingModels={embeddingModels}
          visionModels={visionModels}
          mode={tab}
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
    if (!window.confirm(t('models.confirmDelete', { name: b.display_name }))) {
      return
    }
    try {
      await deletePlatformBinding(token, b.id)
      void load(true)
    } catch (err) {
      window.alert(errorMessage(err, tCode, t))
    }
  }
}

/** The models of a key as the tab shows them: all of them, or only those that read pictures. */
export function shownModels(
  models: string[],
  tab: 'text' | 'vision',
  visionIds: Set<string>,
): string {
  const shown = tab === 'vision' ? models.filter((id) => visionIds.has(id)) : models
  return shown.length > 0 ? shown.join(', ') : '—'
}

function BindingEditorDialog({
  token,
  binding,
  chatModels,
  embeddingModels,
  visionModels,
  mode,
  onClose,
  onSaved,
}: {
  token: string
  binding: PlatformBindingDetail | null
  chatModels: ModelCatalogEntry[]
  embeddingModels: ModelCatalogEntry[]
  visionModels: ModelCatalogEntry[]
  mode: 'text' | 'vision'
  onClose: () => void
  onSaved: () => void
}) {
  const { t, tCode } = useLang()
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
  const [error, setError] = useState<{ err: unknown } | null>(null)

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
      setError({ err })
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
          <strong>{isNew ? t('models.editor.addTitle') : t('models.editor.editTitle', { name: binding.display_name })}</strong>
          <button onClick={onClose} aria-label={t('common.close')} type="button">
            <X size={16} />
          </button>
        </header>
        <form className="admin-drawer-body admin-form" onSubmit={submit}>
          {isNew && (
            <label className="admin-field">
              <span>{t('models.editor.vendor')}</span>
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
                placeholder={t('models.editor.vendorPlaceholder')}
                required
              />
            </label>
          )}
          <label className="admin-field">
            <span>{t('models.editor.displayName')}</span>
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
            />
          </label>
          <label className="admin-field">
            <span>{t('models.col.baseUrl')}</span>
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
            <span>{isNew ? t('models.editor.apiKey') : t('models.editor.apiKeyKeep')}</span>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
            />
          </label>
          <label className="admin-field">
            <span>{t('models.editor.priority')}</span>
            <input
              type="number"
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value) || 100)}
            />
          </label>
          {!isNew && (
            <label className="admin-field">
              <span>{t('models.col.status')}</span>
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {t(`models.status.${s}`)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <div className="admin-field">
            <span>
              {mode === 'vision'
                ? t('models.editor.visionModels')
                : binding?.provider_kind === 'local_embedding'
                  ? t('models.editor.embeddingModels')
                  : t('models.editor.chatModels')}
            </span>
            <div className="admin-model-allowlist">
              {(mode === 'vision'
                ? visionModels
                : binding?.provider_kind === 'local_embedding'
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

          {error && (
            <div className="admin-state admin-state-error">
              {errorMessage(error.err, tCode, t)}
            </div>
          )}

          <div className="admin-form-actions">
            <button type="button" onClick={onClose} disabled={saving}>
              {t('common.cancel')}
            </button>
            <button type="submit" className="admin-button-primary" disabled={saving}>
              {saving ? t('common.saving') : t('common.save')}
            </button>
          </div>
        </form>
      </aside>
    </div>
  )
}
