import { AlertTriangle, Loader2, Plus, Trash2, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useGroups } from '../groups/GroupsContext'
import { useMembers } from '../members/MembersContext'
import { useOrgUnits } from '../orgUnits/OrgUnitsContext'
import { usePositions } from '../positions/PositionsContext'
import { useSpaces } from '../spaces/SpacesContext'

import {
  createAgent,
  grantAgentAccess,
  listAgentAccess,
  listTools,
  revokeAgentAccess,
  updateAgent,
} from './api'
import { AGENT_ICON_KEYS, AgentIcon } from './icons'
import type {
  AgentDetail,
  AgentInput,
  AgentKind,
  AgentSubjectType,
  AgentVisibility,
  KnowledgeScopeMode,
  ToolInfo,
} from './types'

interface Props {
  open: boolean
  agent: AgentDetail | null
  onClose: () => void
  onSaved: (agent: AgentDetail) => void
}

type ExternalProtocol = 'openai_chat' | 'coze' | 'dify'
const EXTERNAL_PROTOCOLS: ExternalProtocol[] = ['openai_chat', 'coze', 'dify']

const SCOPE_MODES: KnowledgeScopeMode[] = ['user', 'intersect', 'fixed']
const SUBJECT_TYPES: AgentSubjectType[] = [
  'user',
  'organization_unit',
  'position',
  'group',
]

/** A visibility grant the editor is tracking (persisted ones carry bindingId). */
interface GrantItem {
  key: string
  subjectType: AgentSubjectType
  subjectId: string
  label: string
  subtitle?: string
  bindingId?: string
}

export function AgentEditorDialog({ open, agent, onClose, onSaved }: Props) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const isEdit = agent !== null

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [icon, setIcon] = useState('bot')
  const [systemPrompt, setSystemPrompt] = useState('')
  const [kind, setKind] = useState<AgentKind>('builtin')
  const [externalProtocol, setExternalProtocol] =
    useState<ExternalProtocol>('openai_chat')
  const [externalEndpoint, setExternalEndpoint] = useState('')
  const [externalModel, setExternalModel] = useState('')
  const [externalBotId, setExternalBotId] = useState('')
  const [externalApiKey, setExternalApiKey] = useState('')
  const [externalApiKeyPresent, setExternalApiKeyPresent] = useState(false)
  const [tools, setTools] = useState<string[]>([])
  const [catalog, setCatalog] = useState<ToolInfo[]>([])
  const [scopeMode, setScopeMode] = useState<KnowledgeScopeMode>('user')
  const [scopeSpaceIds, setScopeSpaceIds] = useState<string[]>([])
  const [visibility, setVisibility] = useState<AgentVisibility>('organization')
  const [grants, setGrants] = useState<GrantItem[]>([])
  const [initialGrants, setInitialGrants] = useState<GrantItem[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const { state: spacesState } = useSpaces()
  const spaces = spacesState.kind === 'ready' ? spacesState.items : []

  useEffect(() => {
    if (!open || !credentials) return
    let cancelled = false
    void listTools(credentials)
      .then((res) => {
        if (!cancelled) setCatalog(res.items)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [open, credentials])

  // Seed form from the agent (or blanks). For an existing restricted agent
  // also load its current access grants.
  useEffect(() => {
    if (!open) return
    setName(agent?.name ?? '')
    setDescription(agent?.description ?? '')
    setIcon(agent?.icon ?? 'bot')
    setSystemPrompt(agent?.system_prompt ?? '')
    setKind(agent?.kind ?? 'builtin')
    setExternalProtocol(
      (agent?.external_protocol as ExternalProtocol) ?? 'openai_chat',
    )
    setExternalEndpoint(agent?.external_endpoint ?? '')
    setExternalModel(agent?.external_model ?? '')
    setExternalBotId(agent?.external_bot_id ?? '')
    setExternalApiKey('')
    setExternalApiKeyPresent(agent?.external_api_key_present ?? false)
    setTools(agent?.enabled_tools ?? [])
    setScopeMode(agent?.knowledge_scope_mode ?? 'user')
    setScopeSpaceIds(agent?.scope_space_ids ?? [])
    setVisibility(agent?.visibility ?? 'organization')
    setGrants([])
    setInitialGrants([])
    setError(null)
    setSubmitting(false)

    if (agent && credentials) {
      void listAgentAccess(credentials, agent.id)
        .then((res) => {
          const items: GrantItem[] = res.items.map((g) => ({
            key: `${g.subject_type}:${g.subject_id}`,
            subjectType: g.subject_type,
            subjectId: g.subject_id,
            label: g.subject_display_name,
            subtitle: g.subject_subtitle ?? undefined,
            bindingId: g.id,
          }))
          setGrants(items)
          setInitialGrants(items)
        })
        .catch(() => undefined)
    }
  }, [open, agent, credentials])

  useEffect(() => {
    if (!open) return
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onEscape)
    return () => window.removeEventListener('keydown', onEscape)
  }, [open, submitting, onClose])

  if (!open) return null

  const toggleTool = (toolName: string) =>
    setTools((prev) =>
      prev.includes(toolName)
        ? prev.filter((x) => x !== toolName)
        : [...prev, toolName],
    )

  const toggleSpace = (spaceId: string) =>
    setScopeSpaceIds((prev) =>
      prev.includes(spaceId)
        ? prev.filter((x) => x !== spaceId)
        : [...prev, spaceId],
    )

  const isExternal = kind === 'external'
  const needsSpaces = !isExternal && scopeMode !== 'user'
  const spacesOk = !needsSpaces || scopeSpaceIds.length > 0
  const grantsOk = visibility !== 'restricted' || grants.length > 0
  const externalOk =
    !isExternal ||
    (externalProtocol === 'openai_chat'
      ? externalEndpoint.trim().length > 0 && externalModel.trim().length > 0
      : externalProtocol === 'coze'
        ? externalBotId.trim().length > 0
        : true) // dify: endpoint defaults; key checked by remote
  const toolsOk = isExternal || tools.length > 0

  const canSubmit =
    name.trim().length > 0 &&
    toolsOk &&
    externalOk &&
    spacesOk &&
    grantsOk &&
    !submitting &&
    !!credentials

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!canSubmit || !credentials) return
    setSubmitting(true)
    setError(null)
    const payload: AgentInput = isExternal
      ? {
          name: name.trim(),
          description: description.trim() || null,
          icon,
          system_prompt: systemPrompt.trim(),
          enabled_tools: [],
          visibility,
          kind: 'external',
          external_protocol: externalProtocol,
          // Endpoint is optional for coze/dify (server fills a default);
          // only send it when the user typed one.
          ...(externalEndpoint.trim()
            ? { external_endpoint: externalEndpoint.trim() }
            : {}),
          ...(externalProtocol === 'openai_chat'
            ? { external_model: externalModel.trim() }
            : {}),
          ...(externalProtocol === 'coze'
            ? { external_bot_id: externalBotId.trim() }
            : {}),
          ...(externalApiKey.trim()
            ? { external_api_key: externalApiKey.trim() }
            : {}),
        }
      : {
          name: name.trim(),
          description: description.trim() || null,
          icon,
          system_prompt: systemPrompt.trim(),
          enabled_tools: tools,
          knowledge_scope_mode: scopeMode,
          scope_space_ids: needsSpaces ? scopeSpaceIds : [],
          visibility,
          kind: 'builtin',
        }
    try {
      const saved = isEdit
        ? await updateAgent(credentials, agent.id, payload)
        : await createAgent(credentials, payload)

      // Reconcile access grants only when the agent is restricted.
      if (visibility === 'restricted') {
        const currentKeys = new Set(grants.map((g) => g.key))
        // Grant the ones not yet persisted.
        for (const g of grants) {
          if (!g.bindingId) {
            await grantAgentAccess(
              credentials,
              saved.id,
              g.subjectType,
              g.subjectId,
            )
          }
        }
        // Revoke the ones removed from the working set.
        for (const ig of initialGrants) {
          if (ig.bindingId && !currentKeys.has(ig.key)) {
            await revokeAgentAccess(credentials, saved.id, ig.bindingId)
          }
        }
      }
      onSaved(saved)
      onClose()
    } catch (err) {
      setError(
        err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      )
    } finally {
      setSubmitting(false)
    }
  }

  const toolLabel = (toolName: string) => {
    const key = `agent.tools.${toolName}`
    const label = t(key)
    return label === key ? toolName : label
  }

  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true">
      <div className="dialog-card agent-editor-card">
        <header className="dialog-header">
          <h2>{isEdit ? t('agent.editor.editTitle') : t('agent.editor.newTitle')}</h2>
          <button
            aria-label={t('agent.editor.cancel')}
            className="dialog-close"
            disabled={submitting}
            onClick={onClose}
            type="button"
          >
            <X size={16} />
          </button>
        </header>
        <form onSubmit={handleSubmit} className="dialog-form agent-editor-form">
          <div className="agent-editor-grid">
            <div className="agent-editor-col">
          <label>
            <span>{t('agent.editor.name')}</span>
            <input
              autoFocus
              onChange={(e) => setName(e.target.value)}
              placeholder={t('agent.editor.namePlaceholder')}
              type="text"
              value={name}
            />
          </label>
          <label>
            <span>{t('agent.editor.description')}</span>
            <input
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('agent.editor.descriptionPlaceholder')}
              type="text"
              value={description}
            />
          </label>

          <div className="agent-editor-field">
            <span className="agent-editor-label">{t('agent.editor.kind.label')}</span>
            <div className="agent-radio-group">
              {(['builtin', 'external'] as AgentKind[]).map((k) => (
                <label
                  key={k}
                  className={
                    kind === k ? 'agent-radio-option active' : 'agent-radio-option'
                  }
                >
                  <input
                    type="radio"
                    name="agentKind"
                    checked={kind === k}
                    onChange={() => setKind(k)}
                  />
                  <span className="agent-radio-body">
                    <strong>{t(`agent.editor.kind.mode.${k}`)}</strong>
                    <small>{t(`agent.editor.kind.modeHint.${k}`)}</small>
                  </span>
                </label>
              ))}
            </div>
          </div>

          {isExternal && (
            <div className="agent-editor-field agent-external-fields">
              <label>
                <span>{t('agent.editor.external.endpoint')}</span>
                <input
                  onChange={(e) => setExternalEndpoint(e.target.value)}
                  placeholder="https://api.example.com/v1"
                  type="text"
                  value={externalEndpoint}
                />
              </label>
              <label>
                <span>{t('agent.editor.external.model')}</span>
                <input
                  onChange={(e) => setExternalModel(e.target.value)}
                  placeholder={t('agent.editor.external.modelPlaceholder')}
                  type="text"
                  value={externalModel}
                />
              </label>
              <label>
                <span>{t('agent.editor.external.apiKey')}</span>
                <input
                  autoComplete="off"
                  onChange={(e) => setExternalApiKey(e.target.value)}
                  placeholder={
                    externalApiKeyPresent
                      ? t('agent.editor.external.apiKeyKeep')
                      : t('agent.editor.external.apiKeyPlaceholder')
                  }
                  type="password"
                  value={externalApiKey}
                />
              </label>
              <p className="agent-empty-hint">{t('agent.editor.external.hint')}</p>
            </div>
          )}

          <div className="agent-editor-field">
            <span className="agent-editor-label">{t('agent.editor.icon')}</span>
            <div className="agent-icon-picker">
              {AGENT_ICON_KEYS.map((key) => (
                <button
                  key={key}
                  type="button"
                  className={
                    key === icon ? 'agent-icon-option active' : 'agent-icon-option'
                  }
                  onClick={() => setIcon(key)}
                  aria-pressed={key === icon}
                  aria-label={key}
                >
                  <AgentIcon icon={key} size={18} />
                </button>
              ))}
            </div>
          </div>

          <label>
            <span>{t('agent.editor.systemPrompt')}</span>
            <textarea
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder={t('agent.editor.systemPromptPlaceholder')}
              rows={3}
              value={systemPrompt}
            />
          </label>

          {!isExternal && (
            <div className="agent-editor-field">
              <span className="agent-editor-label">
                {t('agent.editor.tools')} · {tools.length}
              </span>
              <div className="agent-tool-list">
                {catalog.map((tool) => {
                  const checked = tools.includes(tool.name)
                  return (
                    <label
                      key={tool.name}
                      className={
                        checked ? 'agent-tool-option active' : 'agent-tool-option'
                      }
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleTool(tool.name)}
                      />
                      <span className="agent-tool-option-body">
                        <strong>{toolLabel(tool.name)}</strong>
                        <small>{tool.description}</small>
                      </span>
                    </label>
                  )
                })}
              </div>
            </div>
          )}

            </div>
            <div className="agent-editor-col">
          {/* -------- Knowledge scope (builtin only) -------- */}
          {!isExternal && (
          <div className="agent-editor-field">
            <span className="agent-editor-label">{t('agent.editor.scope.label')}</span>
            <div className="agent-radio-group">
              {SCOPE_MODES.map((mode) => (
                <label
                  key={mode}
                  className={
                    scopeMode === mode
                      ? 'agent-radio-option active'
                      : 'agent-radio-option'
                  }
                >
                  <input
                    type="radio"
                    name="scopeMode"
                    checked={scopeMode === mode}
                    onChange={() => setScopeMode(mode)}
                  />
                  <span className="agent-radio-body">
                    <strong>{t(`agent.editor.scope.mode.${mode}`)}</strong>
                    <small>{t(`agent.editor.scope.modeHint.${mode}`)}</small>
                  </span>
                </label>
              ))}
            </div>
            {scopeMode === 'fixed' && (
              <div className="agent-warning">
                <AlertTriangle size={13} />
                <span>{t('agent.editor.scope.fixedWarning')}</span>
              </div>
            )}
            {needsSpaces && (
              <div className="agent-space-list">
                {spaces.length === 0 && (
                  <p className="agent-empty-hint">{t('agent.editor.scope.noSpaces')}</p>
                )}
                {spaces.map((s) => {
                  const checked = scopeSpaceIds.includes(s.id)
                  return (
                    <label
                      key={s.id}
                      className={
                        checked ? 'agent-tool-option active' : 'agent-tool-option'
                      }
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleSpace(s.id)}
                      />
                      <span className="agent-tool-option-body">
                        <strong>{s.name}</strong>
                        <small>{s.slug}</small>
                      </span>
                    </label>
                  )
                })}
              </div>
            )}
          </div>
          )}

          {/* -------- Visibility -------- */}
          <div className="agent-editor-field">
            <span className="agent-editor-label">
              {t('agent.editor.visibility.label')}
            </span>
            <div className="agent-radio-group">
              {(['organization', 'restricted'] as AgentVisibility[]).map((v) => (
                <label
                  key={v}
                  className={
                    visibility === v
                      ? 'agent-radio-option active'
                      : 'agent-radio-option'
                  }
                >
                  <input
                    type="radio"
                    name="visibility"
                    checked={visibility === v}
                    onChange={() => setVisibility(v)}
                  />
                  <span className="agent-radio-body">
                    <strong>{t(`agent.editor.visibility.mode.${v}`)}</strong>
                    <small>{t(`agent.editor.visibility.modeHint.${v}`)}</small>
                  </span>
                </label>
              ))}
            </div>
            {visibility === 'restricted' && (
              <GrantsManager grants={grants} onChange={setGrants} />
            )}
          </div>
            </div>
          </div>

          {error && <div className="dialog-error">{error}</div>}
          <div className="dialog-actions">
            <button
              className="secondary-button"
              disabled={submitting}
              onClick={onClose}
              type="button"
            >
              {t('agent.editor.cancel')}
            </button>
            <button className="primary-button" disabled={!canSubmit} type="submit">
              {submitting ? <Loader2 className="spin" size={16} /> : null}
              <span>{t('agent.editor.save')}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ========================================================================
 * GrantsManager — pick subjects (users/departments/positions/groups) that
 * may use a restricted agent. Reuses the org contexts already mounted
 * above the workspace.
 * ==================================================================== */

function GrantsManager({
  grants,
  onChange,
}: {
  grants: GrantItem[]
  onChange: (next: GrantItem[]) => void
}) {
  const { t } = useTranslation()
  const { state: members } = useMembers()
  const { units } = useOrgUnits()
  const { positions } = usePositions()
  const { groups } = useGroups()

  const [subjectType, setSubjectType] = useState<AgentSubjectType>('user')
  const [subjectId, setSubjectId] = useState('')

  const candidates = useMemo(() => {
    switch (subjectType) {
      case 'user':
        return members.kind === 'ready'
          ? members.items.map((m) => ({
              id: m.id,
              label: m.display_name,
              subtitle: m.email,
            }))
          : []
      case 'organization_unit':
        return units.kind === 'ready'
          ? units.items
              .filter((u) => u.status !== 'archived')
              .map((u) => ({ id: u.id, label: u.name, subtitle: u.type }))
          : []
      case 'position':
        return positions.kind === 'ready'
          ? positions.items
              .filter((p) => p.status !== 'archived')
              .map((p) => ({
                id: p.id,
                label: p.name,
                subtitle: p.grade ?? p.code ?? '',
              }))
          : []
      case 'group':
        return groups.kind === 'ready'
          ? groups.items
              .filter((g) => g.status !== 'archived')
              .map((g) => ({ id: g.id, label: g.name, subtitle: g.type }))
          : []
      default:
        return []
    }
  }, [subjectType, members, units, positions, groups])

  useEffect(() => {
    setSubjectId(candidates[0]?.id ?? '')
  }, [candidates])

  const add = () => {
    if (!subjectId) return
    const cand = candidates.find((c) => c.id === subjectId)
    if (!cand) return
    const key = `${subjectType}:${subjectId}`
    if (grants.some((g) => g.key === key)) return
    onChange([
      ...grants,
      {
        key,
        subjectType,
        subjectId,
        label: cand.label,
        subtitle: cand.subtitle || undefined,
      },
    ])
  }

  const remove = (key: string) => onChange(grants.filter((g) => g.key !== key))

  return (
    <div className="agent-grants">
      <div className="agent-grants-picker">
        <select
          value={subjectType}
          onChange={(e) => setSubjectType(e.target.value as AgentSubjectType)}
        >
          {SUBJECT_TYPES.map((s) => (
            <option key={s} value={s}>
              {t(`spaceAccess.subjectTypeOption.${s}`)}
            </option>
          ))}
        </select>
        <select value={subjectId} onChange={(e) => setSubjectId(e.target.value)}>
          {candidates.length === 0 && (
            <option value="">{t('agent.editor.visibility.noCandidates')}</option>
          )}
          {candidates.map((c) => (
            <option key={c.id} value={c.id}>
              {c.label}
              {c.subtitle ? ` · ${c.subtitle}` : ''}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="secondary-button"
          onClick={add}
          disabled={!subjectId}
        >
          <Plus size={14} />
          <span>{t('agent.editor.visibility.add')}</span>
        </button>
      </div>
      {grants.length === 0 ? (
        <p className="agent-empty-hint">{t('agent.editor.visibility.noGrants')}</p>
      ) : (
        <ul className="agent-grant-list">
          {grants.map((g) => (
            <li key={g.key} className="agent-grant-row">
              <span className="agent-grant-body">
                <strong>{g.label}</strong>
                <small>
                  {t(`spaceAccess.subjectTypeShort.${g.subjectType}`)}
                  {g.subtitle ? ` · ${g.subtitle}` : ''}
                </small>
              </span>
              <button
                type="button"
                className="icon-button danger"
                onClick={() => remove(g.key)}
                aria-label={t('agent.editor.visibility.removeGrant')}
              >
                <Trash2 size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
