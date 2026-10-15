import { Loader2, Lock, Pencil, Plus, Sparkles, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useAuthModal } from '../auth/AuthModalContext'

import { deleteAgent, getAgent, listAgents } from './api'
import { AgentEditorDialog } from './AgentEditorDialog'
import { AgentIcon } from './icons'
import type { AgentDetail, AgentSummary } from './types'
import { localizedName } from '../../i18n/localizedName'

interface Props {
  onOpen: (agent: AgentSummary) => void
}

type LoadState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: AgentSummary[] }

/**
 * Agent gallery — one card per persona. Clicking a card opens that
 * agent's own conversation; the header "New agent" button and the
 * per-card edit/delete controls manage the org's roster.
 */
export function AgentsListView({ onOpen }: Props) {
  const { t, i18n } = useTranslation()
  const { credentials, isGuest } = useAuth()
  // The seeded demo assistant is named "中文 / English"; guests see the half
  // that matches the interface language. Real users' agents are left as typed.
  const locale = i18n.resolvedLanguage === 'zh-CN' ? 'zh-CN' : 'en-US'
  const shown = (text: string) => (isGuest ? localizedName(text, locale) : text)
  const { openAuth } = useAuthModal()
  const [state, setState] = useState<LoadState>({ kind: 'loading' })
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<AgentDetail | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const reload = useCallback(async () => {
    if (!credentials) return
    setState({ kind: 'loading' })
    try {
      const res = await listAgents(credentials)
      setState({ kind: 'ready', items: res.items })
    } catch (err) {
      setState({
        kind: 'error',
        message:
          err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      })
    }
  }, [credentials])

  useEffect(() => {
    void reload()
  }, [reload])

  const openNew = () => {
    if (isGuest) {
      openAuth()
      return
    }
    setEditing(null)
    setEditorOpen(true)
  }

  const openEdit = async (agentId: string) => {
    if (!credentials) return
    try {
      const detail = await getAgent(credentials, agentId)
      setEditing(detail)
      setEditorOpen(true)
    } catch {
      /* ignore — card stays as-is */
    }
  }

  const remove = async (agent: AgentSummary) => {
    if (!credentials) return
    if (!window.confirm(t('agent.list.confirmDelete', { name: agent.name }))) return
    setBusyId(agent.id)
    try {
      await deleteAgent(credentials, agent.id)
      await reload()
    } catch (err) {
      window.alert(
        err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      )
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="panel agent-panel">
      <div className="agent-panel-header agent-list-header">
        <div>
          <span className="section-label">
            <Sparkles size={15} /> {t('agent.title')}
          </span>
          <p>{t('agent.list.description')}</p>
        </div>
        <button className="primary-button" onClick={openNew} type="button">
          <Plus size={15} />
          <span>{t('agent.list.new')}</span>
        </button>
      </div>

      <div className="agent-list-body">
        {state.kind === 'loading' && (
          <div className="state-panel">{t('agent.list.loading')}</div>
        )}
        {state.kind === 'error' && (
          <div className="state-panel error-state">
            {t('agent.list.loadError')}: {state.message}
          </div>
        )}
        {state.kind === 'ready' && state.items.length === 0 && (
          <div className="state-panel">{t('agent.list.empty')}</div>
        )}
        {state.kind === 'ready' && state.items.length > 0 && (
          <div className="agent-card-grid">
            {state.items.map((agent) => (
              <article
                key={agent.id}
                className="agent-card"
                role="button"
                tabIndex={0}
                onClick={() => onOpen(agent)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onOpen(agent)
                  }
                }}
              >
                <div className="agent-card-top">
                  <span className="agent-card-icon" aria-hidden="true">
                    <AgentIcon icon={agent.icon} size={22} />
                  </span>
                  <div className="agent-card-actions">
                    <button
                      className="icon-button agent-card-action"
                      onClick={(e) => {
                        e.stopPropagation()
                        void openEdit(agent.id)
                      }}
                      title={t('agent.list.edit')}
                      aria-label={t('agent.list.edit')}
                      type="button"
                    >
                      <Pencil size={13} />
                    </button>
                    {!agent.is_default && (
                      <button
                        className="icon-button agent-card-action"
                        onClick={(e) => {
                          e.stopPropagation()
                          void remove(agent)
                        }}
                        title={t('agent.list.delete')}
                        aria-label={t('agent.list.delete')}
                        disabled={busyId === agent.id}
                        type="button"
                      >
                        {busyId === agent.id ? (
                          <Loader2 size={13} className="spin" />
                        ) : (
                          <Trash2 size={13} />
                        )}
                      </button>
                    )}
                  </div>
                </div>
                <h3 className="agent-card-name">
                  {shown(agent.name)}
                  {agent.is_default && (
                    <span className="agent-card-badge">
                      {t('agent.list.defaultBadge')}
                    </span>
                  )}
                  {agent.visibility === 'restricted' && (
                    <span
                      className="agent-card-badge agent-card-badge-lock"
                      title={t('agent.list.restrictedBadge')}
                    >
                      <Lock size={10} /> {t('agent.list.restrictedBadge')}
                    </span>
                  )}
                </h3>
                <p className="agent-card-desc">
                  {agent.description ? shown(agent.description) : t('agent.list.noDescription')}
                </p>
                <div className="agent-card-foot">
                  {t('agent.list.toolCount', { count: agent.enabled_tools.length })}
                </div>
              </article>
            ))}
          </div>
        )}
      </div>

      <AgentEditorDialog
        open={editorOpen}
        agent={editing}
        onClose={() => setEditorOpen(false)}
        onSaved={() => void reload()}
      />
    </section>
  )
}
