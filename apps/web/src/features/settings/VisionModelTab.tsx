import { Eye, ShieldAlert } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import {
  clearModelSelection,
  getOrganizationSettings,
  setModelSelection,
  updateOrganizationSettings,
  type ModelSelectionSlot,
  type UsableChatModel,
} from './api'
import { describeModel } from './modelDescriptions'

// Mirrors the backend's VENDOR_DISPLAY_NAMES (services/ai/registry.py); an unmapped vendor
// shows as the raw key.
const VENDOR_NAMES: Record<string, string> = {
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  dashscope: '通义千问 · DashScope',
  moonshot: 'Kimi · Moonshot',
  zhipu: '智谱 · GLM',
}

const readErr = (err: unknown) =>
  err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

/** Models that can be chosen, grouped by the company that serves them. */
export function groupByVendor(models: UsableChatModel[]): [string, UsableChatModel[]][] {
  const groups = new Map<string, UsableChatModel[]>()
  for (const m of models) {
    const key = m.vendor ? (VENDOR_NAMES[m.vendor] ?? m.vendor) : m.binding_name
    groups.set(key, [...(groups.get(key) ?? []), m])
  }
  return [...groups.entries()]
}

/**
 * The "vision model" tab: which model describes the organisation's pictures, kept separate
 * from the model that answers chats, and the switch that lets pictures be sent to it.
 *
 * Describing a picture sends it to the model's company, so it takes two deliberate steps (choose
 * a model, then turn it on, after reading where the pictures go). Changing or clearing the model
 * turns the switch off again on the server; this tab reloads to show that.
 */
export function VisionModelTab({
  usable,
  selection,
  onChanged,
}: {
  usable: UsableChatModel[]
  selection: ModelSelectionSlot
  onChanged: () => Promise<void> | void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [customBinding, setCustomBinding] = useState('')
  const [customName, setCustomName] = useState('')

  const candidates = useMemo(() => usable.filter((m) => m.supports_vision === true), [usable])
  // Bindings that can carry a model name the administrator types (their own endpoints).
  const bindings = useMemo(() => {
    const seen = new Map<string, string>()
    for (const m of usable) if (!m.is_private) seen.set(m.binding_id, m.binding_name)
    return [...seen.entries()]
  }, [usable])
  const chosen = selection.model_id && selection.binding_id ? selection : null
  const chosenModel = chosen
    ? usable.find((m) => m.model_id === chosen.model_id && m.binding_id === chosen.binding_id)
    : undefined
  const chosenLabel = chosenModel
    ? `${chosenModel.display_name} · ${chosenModel.binding_name}`
    : (chosen?.model_id ?? '')
  const chosenVendor = chosenModel?.vendor
    ? (VENDOR_NAMES[chosenModel.vendor] ?? chosenModel.vendor)
    : (chosenModel?.binding_name ?? t('modelSettings.vision.theirProvider'))

  const loadSwitch = useCallback(async () => {
    if (!credentials) return
    try {
      setEnabled((await getOrganizationSettings(credentials)).image_vision_enabled === true)
    } catch (err) {
      setError(readErr(err))
    }
  }, [credentials])

  useEffect(() => {
    void loadSwitch()
  }, [loadSwitch, chosen?.model_id, chosen?.binding_id])

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await onChanged()
      await loadSwitch()
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  const choose = (modelId: string, bindingId: string) =>
    run(() => setModelSelection(credentials!, 'vision', modelId, bindingId))

  const toggle = async (next: boolean) => {
    if (!credentials || busy) return
    if (next && !window.confirm(t('modelSettings.vision.confirm', { provider: chosenVendor }))) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      const s = await updateOrganizationSettings(credentials, { image_vision_enabled: next })
      setEnabled(s.image_vision_enabled === true)
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="section-label">
              <Eye size={15} /> {t('modelSettings.vision.title')}
            </span>
            <p>{t('modelSettings.vision.description')}</p>
          </div>
        </div>

        <div className={chosen ? 'settings-status' : 'settings-status is-default'}>
          {chosen
            ? t('modelSettings.vision.using', { model: chosenLabel })
            : t('modelSettings.vision.none')}
        </div>

        {candidates.length === 0 && (
          <small className="settings-hint">{t('modelSettings.vision.noModels')}</small>
        )}
        {groupByVendor(candidates).map(([vendor, models]) => (
          <fieldset className="vision-group" key={vendor}>
            <legend>{vendor}</legend>
            {models.map((m) => {
              const isChosen =
                chosen?.model_id === m.model_id && chosen?.binding_id === m.binding_id
              return (
                <label
                  className={isChosen ? 'vision-model is-chosen' : 'vision-model'}
                  key={`${m.binding_id}::${m.model_id}`}
                >
                  <input
                    checked={isChosen}
                    disabled={busy}
                    name="vision-model"
                    onChange={() => void choose(m.model_id, m.binding_id)}
                    type="radio"
                  />
                  <span>
                    <strong>{m.display_name}</strong>
                    <small>{describeModel(m.model_id, t)}</small>
                  </span>
                </label>
              )
            })}
          </fieldset>
        ))}

        {bindings.length > 0 && (
          <div className="dialog-form vision-custom">
            <strong>{t('modelSettings.vision.customTitle')}</strong>
            <small className="settings-hint">{t('modelSettings.vision.customHint')}</small>
            <label>
              <span>{t('modelSettings.vision.customBinding')}</span>
              <select value={customBinding} onChange={(e) => setCustomBinding(e.target.value)}>
                <option value="">{t('modelSettings.vision.customBindingPlaceholder')}</option>
                {bindings.map(([id, name]) => (
                  <option key={id} value={id}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>{t('modelSettings.vision.customName')}</span>
              <input
                onChange={(e) => setCustomName(e.target.value)}
                placeholder="qwen2.5-vl-72b-instruct"
                type="text"
                value={customName}
              />
            </label>
            <div className="settings-actions">
              <button
                className="primary-button"
                disabled={busy || !customBinding || !customName.trim()}
                onClick={() => void choose(customName.trim(), customBinding)}
                type="button"
              >
                {t('modelSettings.vision.customUse')}
              </button>
            </div>
          </div>
        )}

        {chosen && (
          <div className="settings-actions">
            <button
              className="link-button"
              disabled={busy}
              onClick={() => void run(() => clearModelSelection(credentials!, 'vision'))}
              type="button"
            >
              {t('modelSettings.vision.clear')}
            </button>
          </div>
        )}
        {error && <div className="state-panel error-state">{error}</div>}
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="section-label">
              <ShieldAlert size={15} /> {t('modelSettings.vision.switchTitle')}
            </span>
            <p>{t('modelSettings.vision.switchDescription')}</p>
          </div>
        </div>
        <p className="settings-hint vision-disclosure">
          {t('modelSettings.vision.disclosure', { provider: chosenVendor })}
        </p>
        <label className="settings-model-vendoronly">
          <input
            checked={enabled === true}
            disabled={busy || enabled === null || !chosen}
            onChange={(e) => void toggle(e.target.checked)}
            type="checkbox"
          />
          <span>{t('modelSettings.vision.switchLabel')}</span>
        </label>
        {!chosen && <small className="settings-hint">{t('modelSettings.vision.chooseFirst')}</small>}
        <small className="settings-hint settings-hint-muted">
          {t('modelSettings.vision.changeTurnsOff')}
        </small>
      </section>
    </>
  )
}
