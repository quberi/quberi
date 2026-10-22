import {
  Cpu,
  KeyRound,
  Pencil,
  Plus,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { SearchableMultiSelect } from '../../components/SearchableMultiSelect'
import { describeModel } from './modelDescriptions'
import { VisionModelTab } from './VisionModelTab'

import {
  clearModelSelection,
  createProviderBinding,
  deleteProviderBinding,
  getModelAccess,
  getModelSelections,
  getOrganizationSettings,
  listUsableChatModels,
  listProviderBindings,
  probeProviderModels,
  setModelAccess,
  setModelSelection,
  updateOrganizationSettings,
  updateProviderBinding,
  type ModelAccessPrincipal,
  type ModelSelections,
  type ProviderBinding,
  type UsableChatModel,
} from './api'

/**
 * Common OpenAI-compatible providers with their Base URLs, so users don't
 * have to hunt each vendor's docs. "custom" clears the URL for anything else.
 */
const PROVIDER_PRESETS: {
  id: string
  label: string
  baseUrl: string
  // Keywords to auto-filter a fetched (often aggregated) model list down to
  // this vendor's models. Empty = no filter (e.g. aggregators / custom).
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

const readErr = (err: unknown) =>
  err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

// Recommended chat models per vendor keyword — used to pre-select a sensible
// default set so ordinary users don't have to understand every variant.
// Order matters: earlier = more commonly used, shown first in the list.
const RECOMMEND: Record<string, string[]> = {
  deepseek: ['deepseek-chat', 'deepseek-reasoner'],
  qwen: ['qwen-plus', 'qwen-max', 'qwen-flash', 'qwen-turbo'],
  tongyi: ['qwen-plus', 'qwen-max'],
  gpt: ['gpt-4o', 'gpt-4o-mini'],
  moonshot: ['moonshot-v1-8k', 'moonshot-v1-32k'],
  kimi: ['kimi', 'moonshot'],
  glm: ['glm-4-plus', 'glm-4-flash', 'glm-4'],
  zhipu: ['glm-4-plus', 'glm-4'],
}

// Non-chat model ids (embeddings, speech, vision, image, rerank…) — hidden
// from the chat-model list unless the vendor filter is turned off.
const NON_CHAT_RE =
  /(embed|rerank|speech|tts|audio|whisper|voice|vision|-vl|-img|image|ocr|cogview|wanx?|flux|diffusion|paraformer|sambert|cosyvoice|realtime|moderation|rec$)/i

function recommendedSubs(vendorKeywords: string[]): string[] {
  const out = new Set<string>()
  for (const k of vendorKeywords) {
    for (const s of RECOMMEND[k] ?? []) out.add(s.toLowerCase())
  }
  return [...out]
}

/**
 * Model settings — BYOK + default model + (team) access governance.
 *
 * A provider you add carries the models it serves; you pick a *model*
 * (its provider is implied) as the org default. The per-conversation
 * model picker lives in Chat. Team admins can restrict a model to
 * specific people / groups / org-units / positions.
 */
export function ModelSettingsPanel() {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const isTeam = credentials?.tier === 'team'

  const [bindings, setBindings] = useState<ProviderBinding[]>([])
  const [usable, setUsable] = useState<UsableChatModel[]>([])
  const [selections, setSelections] = useState<ModelSelections | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // "Text models" is the page as it always was; "Vision models" is the separate choice of the
  // model that describes pictures.
  const [tab, setTab] = useState<'text' | 'vision'>('text')

  const load = useCallback(async () => {
    if (!credentials) return
    setLoading(true)
    setError(null)
    try {
      const [b, u, s] = await Promise.all([
        listProviderBindings(credentials),
        listUsableChatModels(credentials),
        getModelSelections(credentials),
      ])
      setBindings(b.items)
      setUsable(u.items)
      setSelections(s)
    } catch (err) {
      setError(readErr(err))
    } finally {
      setLoading(false)
    }
  }, [credentials])

  useEffect(() => {
    void load()
  }, [load])

  const orgBindings = bindings.filter((b) => !b.is_platform_default)

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
            <Cpu size={14} /> {t('modelSettings.eyebrow')}
          </span>
          <h1>{t('modelSettings.title')}</h1>
          <p>{t('modelSettings.description')}</p>
        </div>
      </section>

      {loading && <div className="state-panel">{t('modelSettings.loading')}</div>}
      {error && !loading && <div className="state-panel error-state">{error}</div>}

      {!loading && !error && selections && (
        <div className="settings-tabs" role="tablist">
          {(['text', 'vision'] as const).map((id) => (
            <button
              aria-selected={tab === id}
              className={tab === id ? 'active' : ''}
              key={id}
              onClick={() => setTab(id)}
              role="tab"
              type="button"
            >
              {t(`modelSettings.tabs.${id}`)}
            </button>
          ))}
        </div>
      )}

      {!loading && !error && selections && tab === 'vision' && (
        <VisionModelTab
          onChanged={load}
          selection={selections.vision ?? { model_id: null, binding_id: null }}
          usable={usable}
        />
      )}

      {!loading && !error && selections && tab === 'text' && (
        <>
          <DefaultModelCard
            usable={usable}
            selection={selections.chat}
            busy={busy}
            onSave={(modelId, bindingId) =>
              wrap(() =>
                setModelSelection(credentials!, 'chat', modelId, bindingId),
              ).catch(() => {})
            }
            onClear={() =>
              wrap(() => clearModelSelection(credentials!, 'chat')).catch(() => {})
            }
          />

          <ProvidersCard
            orgBindings={orgBindings}
            busy={busy}
            onAdd={(input) => wrap(() => createProviderBinding(credentials!, input).then(() => {}))}
            onUpdate={(id, input) =>
              wrap(() => updateProviderBinding(credentials!, id, input).then(() => {}))
            }
            onDelete={(id) =>
              wrap(() => deleteProviderBinding(credentials!, id)).catch(() => {})
            }
          />

          {isTeam && <GovernanceCard usable={usable} />}
          {isTeam && <ByokToggleCard />}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// BYOK toggle (team only) — whether members may add their own private models
// ---------------------------------------------------------------------------

function ByokToggleCard() {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [allowByok, setAllowByok] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!credentials) return
    let cancelled = false
    getOrganizationSettings(credentials)
      .then((s) => {
        if (!cancelled) setAllowByok(s.allow_byok)
      })
      .catch((err) => {
        if (!cancelled) setError(readErr(err))
      })
    return () => {
      cancelled = true
    }
  }, [credentials])

  const toggle = async (next: boolean) => {
    if (!credentials || busy) return
    setBusy(true)
    setError(null)
    try {
      const s = await updateOrganizationSettings(credentials, { allow_byok: next })
      setAllowByok(s.allow_byok)
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <span className="section-label">
            <KeyRound size={15} /> {t('modelSettings.byok.title')}
          </span>
          <p>{t('modelSettings.byok.description')}</p>
        </div>
      </div>
      {allowByok === null && !error && (
        <div className="state-panel">{t('modelSettings.loading')}</div>
      )}
      {error && <div className="state-panel error-state">{error}</div>}
      {allowByok !== null && (
        <label className="settings-model-vendoronly">
          <input
            checked={allowByok}
            disabled={busy}
            onChange={(e) => void toggle(e.target.checked)}
            type="checkbox"
          />
          <span>{t('modelSettings.byok.toggleLabel')}</span>
        </label>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Default model — one dropdown; the model implies its provider
// ---------------------------------------------------------------------------

function DefaultModelCard({
  usable,
  selection,
  busy,
  onSave,
  onClear,
}: {
  usable: UsableChatModel[]
  selection: { model_id: string | null; binding_id: string | null }
  busy: boolean
  onSave: (modelId: string, bindingId: string) => void
  onClear: () => void
}) {
  const { t } = useTranslation()
  const key = (m: { binding_id: string; model_id: string }) =>
    `${m.binding_id}::${m.model_id}`
  const currentKey =
    selection.model_id && selection.binding_id
      ? `${selection.binding_id}::${selection.model_id}`
      : ''
  const [value, setValue] = useState(currentKey)
  useEffect(() => setValue(currentKey), [currentKey])

  const usingDefault = !selection.model_id
  const platformDefault = usable.find((m) => m.is_platform_default)
  const canSave = Boolean(value) && value !== currentKey && !busy

  const submit = () => {
    const i = value.indexOf('::')
    if (i < 0) return
    onSave(value.slice(i + 2), value.slice(0, i))
  }

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <span className="section-label">
            <Sparkles size={15} /> {t('modelSettings.chat.title')}
          </span>
          <p>{t('modelSettings.chat.description')}</p>
        </div>
      </div>

      <div className={usingDefault ? 'settings-status is-default' : 'settings-status'}>
        {usingDefault
          ? platformDefault
            ? t('modelSettings.chat.usingPlatformNamed', {
                model: platformDefault.display_name,
              })
            : t('modelSettings.chat.usingDefault')
          : t('modelSettings.chat.usingCustom', {
              model:
                usable.find((m) => key(m) === currentKey)?.display_name ??
                selection.model_id,
            })}
      </div>

      <div className="dialog-form">
        <label>
          <span>{t('modelSettings.chat.modelLabel')}</span>
          <select value={value} onChange={(e) => setValue(e.target.value)}>
            <option value="">{t('modelSettings.chat.modelPlaceholder')}</option>
            {usable.map((m) => (
              <option key={key(m)} value={key(m)}>
                {m.display_name} · {m.binding_name}
                {m.is_platform_default ? ` · ${t('modelSettings.platformDefaultBadge')}` : ''}
              </option>
            ))}
          </select>
          {usable.length === 0 && (
            <small className="settings-hint">{t('modelSettings.chat.noModels')}</small>
          )}
        </label>
        <div className="settings-actions">
          <button className="primary-button" disabled={!canSave} onClick={submit} type="button">
            {t('modelSettings.chat.save')}
          </button>
          {!usingDefault && (
            <button className="link-button" disabled={busy} onClick={onClear} type="button">
              {t('modelSettings.chat.useDefault')}
            </button>
          )}
        </div>
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Provider bindings (BYOK) — each carries the models it serves
// ---------------------------------------------------------------------------

function ProvidersCard({
  orgBindings,
  busy,
  onAdd,
  onUpdate,
  onDelete,
}: {
  orgBindings: ProviderBinding[]
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

  const isEditing = mode !== null && mode !== 'add'
  const presetMatch = PROVIDER_PRESETS.find((p) => p.id === preset)?.match ?? []
  // For a custom endpoint (e.g. an aggregator like MaaS) there's no preset
  // keyword, so derive filter keywords from the provider *name* the user
  // gave (they named it "Qwen" → filter to qwen). Drop generic noise tokens.
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
  }

  const beginAdd = () => {
    reset()
    setMode('add')
    setBaseUrl(PROVIDER_PRESETS[0].baseUrl)
  }

  const beginEdit = (b: ProviderBinding) => {
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
      const res = await probeProviderModels(credentials, body)
      setFetched(res.models)
      if (res.models.length === 0) {
        setFetchErr(t('modelSettings.providers.noModelsFetched'))
      } else if (!isEditing && enabled.length === 0) {
        // Pre-select recommended chat models so ordinary users can just save.
        const subs = recommendedSubs(vendorKeywords)
        const picks = res.models.filter((m) => subs.includes(m.toLowerCase()))
        if (picks.length > 0) setEnabled(picks)
      }
    } catch (err) {
      setFetchErr(readErr(err))
    } finally {
      setFetching(false)
    }
  }

  const submit = async () => {
    if (!displayName.trim()) return
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
    } catch {
      /* surfaced by parent */
    }
  }

  const presetOptions = PROVIDER_PRESETS.map((p) => ({
    id: p.id,
    label: p.id === 'custom' ? t('modelSettings.providers.presetCustom') : p.label,
  }))
  const recSubs = recommendedSubs(vendorKeywords)
  // Exact alias match only — so "qwen-flash" doesn't also flag
  // "qwen-flash-character" or dated snapshots as recommended.
  const isRecommended = (m: string) => recSubs.includes(m.toLowerCase())

  const modelChoices = fetched ?? enabled
  const shownModels = modelChoices
    .filter((m) => {
      const lm = m.toLowerCase()
      if (vendorOnly && vendorKeywords.length > 0) {
        // Only this vendor's *chat* models (hide embeddings/speech/vision…).
        if (!vendorKeywords.some((k) => lm.includes(k))) return false
        if (NON_CHAT_RE.test(lm) && !isRecommended(m)) return false
      }
      if (modelFilter.trim() && !lm.includes(modelFilter.trim().toLowerCase())) {
        return false
      }
      return true
    })
    // Most commonly used (recommended, in preference order) first, then the
    // rest alphabetically.
    .sort((a, b) => {
      const ia = recSubs.indexOf(a.toLowerCase())
      const ib = recSubs.indexOf(b.toLowerCase())
      const ra = ia === -1 ? Number.MAX_SAFE_INTEGER : ia
      const rb = ib === -1 ? Number.MAX_SAFE_INTEGER : ib
      return ra !== rb ? ra - rb : a.localeCompare(b)
    })

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <span className="section-label">
            <KeyRound size={15} /> {t('modelSettings.providers.title')}
          </span>
          <p>{t('modelSettings.providers.description')}</p>
        </div>
        {mode === null && (
          <button className="ghost-button" onClick={beginAdd} type="button">
            <Plus size={14} /> {t('modelSettings.providers.add')}
          </button>
        )}
      </div>

      <ul className="settings-binding-list">
        {orgBindings.map((b) => (
          <li key={b.id} className="settings-binding-row">
            <span className="settings-binding-body">
              <strong>{b.display_name}</strong>
              <small>
                {b.provider_kind}
                {b.base_url ? ` · ${b.base_url}` : ''}
                {b.api_key_present ? ` · ${t('modelSettings.providers.keySet')}` : ''}
                {b.models && b.models.length > 0
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
        {orgBindings.length === 0 && (
          <li className="state-panel">{t('modelSettings.providers.empty')}</li>
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
              placeholder={isEditing ? t('modelSettings.providers.keyKeepPlaceholder') : 'sk-...'}
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
            {fetched && recSubs.length > 0 && enabled.some(isRecommended) && (
              <p className="settings-hint">{t('modelSettings.providers.recommendedHint')}</p>
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
                      <span className="settings-model-id">
                        {m}
                        {isRecommended(m) && (
                          <span className="settings-model-rec">
                            {t('modelSettings.providers.recommended')}
                          </span>
                        )}
                      </span>
                      <small className="settings-model-desc">
                        {describeModel(m, t)}
                      </small>
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
              : t('modelSettings.providers.hint')}
          </small>
        </div>
        </div>
        </div>,
          document.body,
        )}

      {orgBindings.length === 0 && mode === null && (
        <div className="settings-status is-default">
          {t('modelSettings.providers.byokHint')}
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Access governance (team only) — who may use each model
// ---------------------------------------------------------------------------

type PrincipalType = 'user' | 'group' | 'organization_unit' | 'position'

interface PrincipalOption {
  id: string
  label: string
}

function GovernanceCard({ usable }: { usable: UsableChatModel[] }) {
  const { t } = useTranslation()
  const { credentials } = useAuth()

  const [opts, setOpts] = useState<Record<PrincipalType, PrincipalOption[]>>({
    user: [],
    group: [],
    organization_unit: [],
    position: [],
  })
  const [editing, setEditing] = useState<string | null>(null) // binding::model
  // Allow-list selections, one per principal type.
  const [selected, setSelected] = useState<Record<PrincipalType, string[]>>({
    user: [],
    group: [],
    organization_unit: [],
    position: [],
  })
  // Deny-list — member-only by design (see the discussion that led here:
  // excluding one specific person from an otherwise-allowed group/unit/
  // position doesn't need group/unit/position-level deny, just a person).
  const [deniedUsers, setDeniedUsers] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!credentials) return
    let cancelled = false
    Promise.all([
      import('../members/api').then((m) => m.listMembers(credentials)),
      import('../groups/api').then((m) => m.listGroups(credentials)),
      import('../orgUnits/api').then((m) => m.listOrgUnits(credentials)),
      import('../positions/api').then((m) => m.listPositions(credentials)),
    ])
      .then(([members, groups, units, positions]) => {
        if (cancelled) return
        setOpts({
          user: members.items.map((x) => ({ id: x.id, label: x.display_name || x.email })),
          group: groups.items.map((x) => ({ id: x.id, label: x.name })),
          organization_unit: units.items.map((x) => ({ id: x.id, label: x.name })),
          position: positions.items.map((x) => ({ id: x.id, label: x.name })),
        })
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [credentials])

  const openEditor = async (m: UsableChatModel) => {
    if (!credentials) return
    setError(null)
    const key = `${m.binding_id}::${m.model_id}`
    setEditing(key)
    setSelected({ user: [], group: [], organization_unit: [], position: [] })
    setDeniedUsers([])
    try {
      const res = await getModelAccess(credentials, m.binding_id, m.model_id)
      const next: Record<PrincipalType, string[]> = {
        user: [],
        group: [],
        organization_unit: [],
        position: [],
      }
      const denied: string[] = []
      for (const p of res.principals) {
        if (p.effect === 'deny') {
          // Deny is member-only in this UI (see ``deniedUsers`` above);
          // a stray deny on a non-user principal from an older payload
          // is dropped rather than crashing the editor.
          if (p.principal_type === 'user') denied.push(p.principal_id)
          continue
        }
        next[p.principal_type].push(p.principal_id)
      }
      setSelected(next)
      setDeniedUsers(denied)
    } catch (err) {
      setError(readErr(err))
    }
  }

  const save = async (m: UsableChatModel) => {
    if (!credentials) return
    setBusy(true)
    setError(null)
    const allowPrincipals: ModelAccessPrincipal[] = (
      ['user', 'group', 'organization_unit', 'position'] as PrincipalType[]
    ).flatMap((tp) =>
      selected[tp]
        // A user can't be both allowed-by-member and denied at once — the
        // backend rejects that combination outright. Deny wins client-side
        // too, so toggling someone into "exclude" silently drops any
        // direct allow pick for the same person instead of surfacing a
        // confusing 400 on save.
        .filter((id) => tp !== 'user' || !deniedUsers.includes(id))
        .map((id) => ({
          principal_type: tp,
          principal_id: id,
          effect: 'allow' as const,
        })),
    )
    const denyPrincipals: ModelAccessPrincipal[] = deniedUsers.map((id) => ({
      principal_type: 'user' as const,
      principal_id: id,
      effect: 'deny' as const,
    }))
    try {
      await setModelAccess(credentials, {
        binding_id: m.binding_id,
        model_id: m.model_id,
        principals: [...allowPrincipals, ...denyPrincipals],
      })
      setEditing(null)
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  const setForType = (tp: PrincipalType, ids: string[]) =>
    setSelected((prev) => ({ ...prev, [tp]: ids }))

  const totalSelected = (s: Record<PrincipalType, string[]>) =>
    s.user.length + s.group.length + s.organization_unit.length + s.position.length

  const memberOptions = opts.user

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <span className="section-label">
            <ShieldCheck size={15} /> {t('modelSettings.access.title')}
          </span>
          <p>{t('modelSettings.access.description')}</p>
        </div>
      </div>

      {usable.length === 0 && (
        <div className="settings-status is-default">{t('modelSettings.access.empty')}</div>
      )}

      <ul className="settings-binding-list">
        {usable.map((m) => {
          const key = `${m.binding_id}::${m.model_id}`
          const isOpen = editing === key
          return (
            <li key={key} className="settings-access-row">
              <div className="settings-binding-row" style={{ border: 'none', padding: 0 }}>
                <span className="settings-binding-body">
                  <strong>{m.display_name}</strong>
                  <small>{m.binding_name}</small>
                </span>
                {!isOpen && (
                  <button
                    className="ghost-button"
                    onClick={() => void openEditor(m)}
                    type="button"
                  >
                    {t('modelSettings.access.edit')}
                  </button>
                )}
              </div>
              {isOpen && (
                <div className="dialog-form settings-add-form">
                  <p className="settings-hint">
                    {totalSelected(selected) === 0
                      ? deniedUsers.length > 0
                        ? t('modelSettings.access.openExceptDeniedHint', {
                            count: deniedUsers.length,
                          })
                        : t('modelSettings.access.openHint')
                      : t('modelSettings.access.restrictedHint')}
                  </p>
                  <p className="settings-hint settings-hint-muted">
                    {t('modelSettings.access.orLogicHint')}
                  </p>
                  {(
                    [
                      [
                        'user',
                        t('modelSettings.access.byUsers'),
                        t('modelSettings.access.usersPlaceholder'),
                      ],
                      [
                        'group',
                        t('modelSettings.access.byGroups'),
                        t('modelSettings.access.groupsPlaceholder'),
                      ],
                      [
                        'organization_unit',
                        t('modelSettings.access.byOrgUnits'),
                        t('modelSettings.access.orgUnitsPlaceholder'),
                      ],
                      [
                        'position',
                        t('modelSettings.access.byPositions'),
                        t('modelSettings.access.positionsPlaceholder'),
                      ],
                    ] as [PrincipalType, string, string][]
                  ).map(([tp, label, placeholder]) => (
                    <label key={tp}>
                      <span>
                        {label}
                        <small className="settings-optional-tag">
                          {t('modelSettings.access.optionalTag')}
                        </small>
                      </span>
                      <SearchableMultiSelect
                        options={opts[tp]}
                        selected={selected[tp]}
                        onChange={(ids) => setForType(tp, ids)}
                        placeholder={placeholder}
                        searchPlaceholder={t('modelSettings.access.searchPlaceholder')}
                        noResultsLabel={t('modelSettings.access.noResults')}
                        clearLabel={t('modelSettings.access.clearSelection')}
                      />
                    </label>
                  ))}
                  <div className="settings-deny-block">
                    <label>
                      <span>
                        {t('modelSettings.access.excludeUsers')}
                        <small className="settings-optional-tag">
                          {t('modelSettings.access.optionalTag')}
                        </small>
                      </span>
                      <SearchableMultiSelect
                        options={memberOptions}
                        selected={deniedUsers}
                        onChange={setDeniedUsers}
                        placeholder={t('modelSettings.access.excludeUsersPlaceholder')}
                        searchPlaceholder={t('modelSettings.access.searchPlaceholder')}
                        noResultsLabel={t('modelSettings.access.noResults')}
                        clearLabel={t('modelSettings.access.clearSelection')}
                      />
                    </label>
                    <p className="settings-hint settings-hint-muted">
                      {t('modelSettings.access.excludeHint')}
                    </p>
                  </div>
                  {error && <div className="signin-error">{error}</div>}
                  <div className="settings-actions">
                    <button
                      className="primary-button"
                      disabled={busy}
                      onClick={() => void save(m)}
                      type="button"
                    >
                      {t('modelSettings.access.save')}
                    </button>
                    <button
                      className="link-button"
                      onClick={() => setEditing(null)}
                      type="button"
                    >
                      {t('modelSettings.access.cancel')}
                    </button>
                  </div>
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
