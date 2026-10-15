import { Cpu, KeyRound, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import { describeModel } from './modelDescriptions'
import {
  createMyProvider,
  deleteMyProvider,
  listMyProviders,
  probeMyModels,
  updateMyProvider,
  type PrivateProviderBinding,
} from './myModelsApi'

/** Same vendor presets as the admin console's org-wide model manager, kept
 * as a local copy rather than a shared import: the two panels are
 * deliberately independent (different API surface, different audience)
 * and this list is small enough that duplicating it avoids coupling them
 * for a cosmetic convenience. */
const PROVIDER_PRESETS: {
  id: string
  label: string
  baseUrl: string
  match: string[]
}[] = [
  { id: 'deepseek', label: 'DeepSeek', baseUrl: 'https://api.deepseek.com', match: ['deepseek'] },
  {
    id: 'openai',
    label: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    match: ['gpt', 'o1', 'o3', 'o4', 'chatgpt', 'omni', 'text-embedding'],
  },
  {
    id: 'dashscope',
    label: '通义千问 · DashScope',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    match: ['qwen', 'tongyi', 'qwq', 'qvq'],
  },
  {
    id: 'moonshot',
    label: 'Kimi · Moonshot',
    baseUrl: 'https://api.moonshot.cn/v1',
    match: ['moonshot', 'kimi'],
  },
  {
    id: 'zhipu',
    label: '智谱 · GLM',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    match: ['glm', 'zhipu', 'cogview'],
  },
  {
    id: 'siliconflow',
    label: '硅基流动 · SiliconFlow',
    baseUrl: 'https://api.siliconflow.cn/v1',
    match: [],
  },
  { id: 'custom', label: '', baseUrl: '', match: [] },
]

const NON_CHAT_RE =
  /(embed|rerank|speech|tts|audio|whisper|voice|vision|-vl|-img|image|ocr|cogview|wanx?|flux|diffusion|paraformer|sambert|cosyvoice|realtime|moderation|rec$)/i

const readErr = (err: unknown) =>
  err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

/**
 * "My models" — personal BYOK, visible to any signed-in member regardless
 * of org tier or role. Private: the key and the binding itself are never
 * visible to org admins (see services.ai_providers.list_private_bindings_for_identity
 * on the backend — every query is scoped by the caller's global identity_id,
 * so the same person's private models follow them across every org and
 * their personal account). Rejected with a clear message if the *current*
 * org has turned private models off (``organizations.allow_byok``).
 */
export function MyModelsPanel() {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [bindings, setBindings] = useState<PrivateProviderBinding[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    if (!credentials) return
    setLoading(true)
    setError(null)
    try {
      const { items } = await listMyProviders(credentials)
      setBindings(items)
    } catch (err) {
      setError(readErr(err))
    } finally {
      setLoading(false)
    }
  }, [credentials])

  useEffect(() => {
    void load()
  }, [load])

  const wrap = async (fn: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await load()
    } catch (err) {
      setError(readErr(err))
      throw err
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="settings-page">
      <section className="hero hero-compact">
        <div className="hero-copy">
          <span className="eyebrow">
            <Cpu size={14} /> {t('myModels.eyebrow')}
          </span>
          <h1>{t('myModels.title')}</h1>
          <p>{t('myModels.description')}</p>
        </div>
      </section>

      {loading && <div className="state-panel">{t('myModels.loading')}</div>}
      {error && !loading && <div className="state-panel error-state">{error}</div>}

      {!loading && (
        <MyProvidersCard
          bindings={bindings}
          busy={busy}
          onAdd={(input) => wrap(() => createMyProvider(credentials!, input).then(() => {}))}
          onUpdate={(id, input) =>
            wrap(() => updateMyProvider(credentials!, id, input).then(() => {}))
          }
          onDelete={(id) => wrap(() => deleteMyProvider(credentials!, id)).catch(() => {})}
        />
      )}
    </div>
  )
}

function MyProvidersCard({
  bindings,
  busy,
  onAdd,
  onUpdate,
  onDelete,
}: {
  bindings: PrivateProviderBinding[]
  busy: boolean
  onAdd: (input: {
    provider_kind: string
    display_name: string
    base_url?: string | null
    api_key?: string | null
    models?: string[]
  }) => Promise<void>
  onUpdate: (
    id: string,
    input: { display_name?: string; base_url?: string | null; api_key?: string; models?: string[] },
  ) => Promise<void>
  onDelete: (id: string) => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [mode, setMode] = useState<'add' | string | null>(null)
  const [preset, setPreset] = useState('deepseek')
  const [displayName, setDisplayName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [fetched, setFetched] = useState<string[] | null>(null)
  const [enabled, setEnabled] = useState<string[]>([])
  const [manual, setManual] = useState('')
  const [fetching, setFetching] = useState(false)
  const [fetchErr, setFetchErr] = useState<string | null>(null)
  const [modelFilter, setModelFilter] = useState('')
  const [vendorOnly, setVendorOnly] = useState(true)
  const [formError, setFormError] = useState<string | null>(null)

  const isEditing = mode !== null && mode !== 'add'
  const presetMatch = PROVIDER_PRESETS.find((p) => p.id === preset)?.match ?? []
  const NAME_STOP = new Set(['key', 'api', 'the', 'com', 'llm', 'model', 'chat', 'my'])
  const nameKeywords =
    preset === 'custom'
      ? (displayName.toLowerCase().match(/[a-z0-9]{3,}/g) ?? []).filter(
          (k) => !NAME_STOP.has(k),
        )
      : []
  const vendorKeywords = presetMatch.length > 0 ? presetMatch : nameKeywords

  const reset = () => {
    setMode(null)
    setPreset('deepseek')
    setDisplayName('')
    setBaseUrl('')
    setApiKey('')
    setFetched(null)
    setEnabled([])
    setManual('')
    setFetchErr(null)
    setModelFilter('')
    setFormError(null)
  }

  const beginAdd = () => {
    reset()
    setMode('add')
    setBaseUrl(PROVIDER_PRESETS[0].baseUrl)
  }

  const beginEdit = (b: PrivateProviderBinding) => {
    reset()
    setMode(b.id)
    const match = PROVIDER_PRESETS.find((p) => p.baseUrl && p.baseUrl === b.base_url)
    setPreset(match ? match.id : 'custom')
    setDisplayName(b.display_name)
    setBaseUrl(b.base_url ?? '')
    setEnabled(b.models ?? [])
  }

  const applyPreset = (id: string) => {
    setPreset(id)
    setVendorOnly(true)
    const p = PROVIDER_PRESETS.find((x) => x.id === id)
    if (p && p.id !== 'custom') setBaseUrl(p.baseUrl)
  }

  const toggle = (m: string) =>
    setEnabled((prev) => (prev.includes(m) ? prev.filter((x) => x !== m) : [...prev, m]))

  const addManual = () => {
    const v = manual.trim()
    if (!v) return
    if (!enabled.includes(v)) setEnabled((prev) => [...prev, v])
    setFetched((prev) => (prev && !prev.includes(v) ? [...prev, v] : prev))
    setManual('')
  }

  const fetchModels = async () => {
    if (!credentials) return
    setFetching(true)
    setFetchErr(null)
    try {
      const body = isEditing
        ? { binding_id: mode as string, base_url: baseUrl.trim() || undefined }
        : { base_url: baseUrl.trim(), api_key: apiKey.trim() || undefined }
      const res = await probeMyModels(credentials, body)
      setFetched(res.models)
      if (res.models.length === 0) {
        setFetchErr(t('modelSettings.providers.noModelsFetched'))
      }
    } catch (err) {
      setFetchErr(readErr(err))
    } finally {
      setFetching(false)
    }
  }

  const submit = async () => {
    if (!displayName.trim()) return
    setFormError(null)
    try {
      if (isEditing) {
        await onUpdate(mode as string, {
          display_name: displayName.trim(),
          base_url: baseUrl.trim() || null,
          models: enabled,
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        })
      } else {
        if (!apiKey.trim()) return
        await onAdd({
          provider_kind: 'openai_compat',
          display_name: displayName.trim(),
          base_url: baseUrl.trim() || null,
          api_key: apiKey.trim(),
          models: enabled,
        })
      }
      reset()
    } catch (err) {
      setFormError(readErr(err))
    }
  }

  const presetOptions = PROVIDER_PRESETS.map((p) => ({
    id: p.id,
    label: p.id === 'custom' ? t('modelSettings.providers.presetCustom') : p.label,
  }))

  const modelChoices = fetched ?? enabled
  const shownModels = modelChoices.filter((m) => {
    const lm = m.toLowerCase()
    if (vendorOnly && vendorKeywords.length > 0) {
      if (!vendorKeywords.some((k) => lm.includes(k))) return false
      if (NON_CHAT_RE.test(lm)) return false
    }
    if (modelFilter.trim() && !lm.includes(modelFilter.trim().toLowerCase())) return false
    return true
  })

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <span className="section-label">
            <KeyRound size={15} /> {t('myModels.providers.title')}
          </span>
          <p>{t('myModels.providers.description')}</p>
        </div>
        {mode === null && (
          <button className="ghost-button" onClick={beginAdd} type="button">
            <Plus size={14} /> {t('modelSettings.providers.add')}
          </button>
        )}
      </div>

      <ul className="settings-binding-list">
        {bindings.map((b) => (
          <li key={b.id} className="settings-binding-row">
            <span className="settings-binding-body">
              <strong>{b.display_name}</strong>
              <small>
                {b.provider_kind}
                {b.base_url ? ` · ${b.base_url}` : ''}
                {b.api_key_present ? ` · ${t('modelSettings.providers.keySet')}` : ''}
                {b.models.length > 0
                  ? ` · ${t('modelSettings.providers.modelCount', { count: b.models.length })}`
                  : ''}
              </small>
            </span>
            <span className="settings-binding-actions">
              <button
                className="ghost-button"
                disabled={busy || mode !== null}
                onClick={() => beginEdit(b)}
                type="button"
              >
                <Pencil size={13} /> {t('modelSettings.providers.edit')}
              </button>
              <button
                aria-label={t('modelSettings.providers.remove')}
                className="icon-button danger"
                disabled={busy || mode !== null}
                onClick={() => onDelete(b.id)}
                title={t('modelSettings.providers.remove')}
                type="button"
              >
                <Trash2 size={14} />
              </button>
            </span>
          </li>
        ))}
        {bindings.length === 0 && (
          <li className="state-panel">{t('myModels.providers.empty')}</li>
        )}
      </ul>

      {mode !== null &&
        createPortal(
          <div className="dialog-backdrop" role="dialog" aria-modal="true">
            <div className="dialog-card dialog-card-wide">
              <header className="dialog-header">
                <h2>
                  <KeyRound size={16} />{' '}
                  {isEditing
                    ? t('modelSettings.providers.editTitle')
                    : t('modelSettings.providers.addTitle')}
                </h2>
                <button
                  aria-label={t('modelSettings.providers.cancel')}
                  className="dialog-close"
                  onClick={reset}
                  type="button"
                >
                  <X size={16} />
                </button>
              </header>
              <div className="dialog-form settings-add-form">
                <label>
                  <span>{t('modelSettings.providers.presetLabel')}</span>
                  <select value={preset} onChange={(e) => applyPreset(e.target.value)}>
                    {presetOptions.map((o) => (
                      <option key={o.id} value={o.id}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>{t('modelSettings.providers.nameLabel')}</span>
                  <input
                    autoFocus
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder={t('modelSettings.providers.namePlaceholder')}
                    type="text"
                    value={displayName}
                  />
                </label>
                <label>
                  <span>{t('modelSettings.providers.baseUrlLabel')}</span>
                  <input
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder="https://api.deepseek.com"
                    type="text"
                    value={baseUrl}
                  />
                </label>
                <label>
                  <span>{t('modelSettings.providers.keyLabel')}</span>
                  <input
                    autoComplete="off"
                    onChange={(e) => setApiKey(e.target.value)}
                    placeholder={
                      isEditing ? t('modelSettings.providers.keyKeepPlaceholder') : 'sk-...'
                    }
                    type="password"
                    value={apiKey}
                  />
                </label>

                <div className="settings-models-block">
                  <div className="settings-models-head">
                    <span>{t('modelSettings.providers.modelsLabel')}</span>
                    <button
                      className="ghost-button"
                      disabled={fetching || !baseUrl.trim() || (!isEditing && !apiKey.trim())}
                      onClick={fetchModels}
                      type="button"
                      title={
                        !isEditing && !apiKey.trim()
                          ? t('modelSettings.providers.fetchNeedsKey')
                          : undefined
                      }
                    >
                      <RefreshCw size={13} className={fetching ? 'spin' : undefined} />
                      {t('modelSettings.providers.fetchModels')}
                    </button>
                  </div>
                  {fetchErr && <div className="settings-hint error-text">{fetchErr}</div>}
                  {!isEditing && !apiKey.trim() && (
                    <div className="settings-hint">
                      {t('modelSettings.providers.fetchNeedsKey')}
                    </div>
                  )}
                  {modelChoices.length > 0 && (
                    <div className="settings-models-controls">
                      {modelChoices.length > 6 && (
                        <input
                          className="settings-model-search"
                          onChange={(e) => setModelFilter(e.target.value)}
                          placeholder={t('modelSettings.providers.searchModels')}
                          type="search"
                          value={modelFilter}
                        />
                      )}
                      {vendorKeywords.length > 0 && (
                        <label className="settings-model-vendoronly">
                          <input
                            checked={vendorOnly}
                            onChange={(e) => setVendorOnly(e.target.checked)}
                            type="checkbox"
                          />
                          <span>{t('modelSettings.providers.vendorOnly')}</span>
                        </label>
                      )}
                    </div>
                  )}
                  {modelChoices.length > 0 && (
                    <div className="settings-models-grid">
                      {shownModels.map((m) => (
                        <label key={m} className="settings-model-check">
                          <input
                            checked={enabled.includes(m)}
                            onChange={() => toggle(m)}
                            type="checkbox"
                          />
                          <span className="settings-model-name">
                            <span className="settings-model-id">{m}</span>
                            <small className="settings-model-desc">{describeModel(m, t)}</small>
                          </span>
                        </label>
                      ))}
                      {shownModels.length === 0 && (
                        <p className="settings-hint">
                          {t('modelSettings.providers.noneMatchVendor')}
                        </p>
                      )}
                    </div>
                  )}
                  {enabled.length > 0 && (
                    <small className="settings-hint">
                      {t('modelSettings.providers.enabledCount', { count: enabled.length })}
                    </small>
                  )}
                  <div className="settings-model-manual">
                    <input
                      onChange={(e) => setManual(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          addManual()
                        }
                      }}
                      placeholder={t('modelSettings.providers.manualPlaceholder')}
                      type="text"
                      value={manual}
                    />
                    <button className="ghost-button" onClick={addManual} type="button">
                      <Plus size={13} /> {t('modelSettings.providers.addModel')}
                    </button>
                  </div>
                </div>

                {formError && <div className="dialog-error">{formError}</div>}
                <div className="settings-actions">
                  <button
                    className="primary-button"
                    disabled={!displayName.trim() || (!isEditing && !apiKey.trim()) || busy}
                    onClick={submit}
                    type="button"
                  >
                    {isEditing
                      ? t('modelSettings.providers.saveEdit')
                      : t('modelSettings.providers.save')}
                  </button>
                  <button className="link-button" onClick={reset} type="button">
                    {t('modelSettings.providers.cancel')}
                  </button>
                </div>
                <small className="settings-hint">
                  {isEditing
                    ? t('modelSettings.providers.keyKeepHint')
                    : t('myModels.providers.hint')}
                </small>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </section>
  )
}
