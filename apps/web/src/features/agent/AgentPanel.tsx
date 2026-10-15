import {
  ArrowLeft,
  Bot,
  ChevronDown,
  ChevronUp,
  Loader2,
  Send,
  UserRound,
  Wrench,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/AuthContext'

import { streamAgentRun } from './api'
import type { AgentStreamEvent, ToolCallArgs } from './api'
import { AgentIcon } from './icons'
import type { AgentSummary } from './types'

/**
 * Agent panel — the "跃迁" surface.
 *
 * Unlike Chat (single-shot RAG over one space set), the agent reasons
 * across sources: it can call several tools in sequence — search
 * knowledge, look up the org chart, count a department, check who can
 * see a space — and combine the results. This panel makes that
 * reasoning *visible*: every tool call and its result renders inline as
 * the run streams, followed by the final answer token by token.
 */

type StepKind = 'note' | 'tool_call' | 'tool_result'

interface RunStep {
  kind: StepKind
  tool?: string
  arguments?: ToolCallArgs
  result?: Record<string, unknown>
  text?: string
}

interface AgentRun {
  id: string
  query: string
  steps: RunStep[]
  answer: string
  status: 'running' | 'done' | 'error'
  error?: string
  meta?: { iterations?: number; toolCalls?: number; modelId?: string }
}

const SUGGESTIONS_KEYS = ['s1', 's2', 's3'] as const

export function AgentPanel({
  agent,
  onBack,
}: {
  agent: AgentSummary
  onBack: () => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [runs, setRuns] = useState<AgentRun[]>([])
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  // Keep the transcript pinned to the newest content as it streams.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [runs])

  useEffect(() => {
    return () => abortRef.current?.abort()
  }, [])

  const runAgent = async (rawQuery: string) => {
    const q = rawQuery.trim()
    if (!q || busy || !credentials) return
    setQuery('')
    setBusy(true)

    const runId = `${Date.now()}`
    setRuns((prev) => [
      ...prev,
      { id: runId, query: q, steps: [], answer: '', status: 'running' },
    ])

    const patch = (fn: (run: AgentRun) => AgentRun) =>
      setRuns((prev) => prev.map((r) => (r.id === runId ? fn(r) : r)))

    const controller = new AbortController()
    abortRef.current = controller

    try {
      for await (const event of streamAgentRun(
        credentials,
        agent.id,
        q,
        controller.signal,
      )) {
        applyEvent(patch, event)
      }
      patch((r) => (r.status === 'running' ? { ...r, status: 'done' } : r))
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      patch((r) => ({ ...r, status: 'error', error: message }))
    } finally {
      setBusy(false)
      abortRef.current = null
    }
  }

  return (
    <section className="panel agent-panel">
      <div className="agent-panel-header agent-conversation-header">
        <button
          className="icon-button agent-back"
          onClick={onBack}
          type="button"
          aria-label={t('agent.back')}
        >
          <ArrowLeft size={16} />
        </button>
        <span className="agent-conversation-icon" aria-hidden="true">
          <AgentIcon icon={agent.icon} size={20} />
        </span>
        <div>
          <span className="section-label agent-conversation-name">
            {agent.name}
          </span>
          <p>{agent.description || t('agent.description')}</p>
        </div>
      </div>

      <div className="agent-transcript" ref={scrollRef}>
        {runs.length === 0 && <EmptyState onPick={(q) => void runAgent(q)} />}
        {runs.map((run) => (
          <RunView key={run.id} run={run} />
        ))}
      </div>

      <Composer
        value={query}
        onChange={setQuery}
        onSubmit={() => void runAgent(query)}
        busy={busy}
      />
    </section>
  )
}

function applyEvent(
  patch: (fn: (run: AgentRun) => AgentRun) => void,
  event: AgentStreamEvent,
) {
  switch (event.kind) {
    case 'note':
      if (event.text.trim())
        patch((r) => ({ ...r, steps: [...r.steps, { kind: 'note', text: event.text }] }))
      break
    case 'tool_call':
      patch((r) => ({
        ...r,
        steps: [
          ...r.steps,
          { kind: 'tool_call', tool: event.tool, arguments: event.arguments },
        ],
      }))
      break
    case 'tool_result':
      patch((r) => ({
        ...r,
        steps: [
          ...r.steps,
          { kind: 'tool_result', tool: event.tool, result: event.result },
        ],
      }))
      break
    case 'delta':
      patch((r) => ({ ...r, answer: r.answer + event.text }))
      break
    case 'error':
      patch((r) => ({ ...r, status: 'error', error: event.message }))
      break
    case 'done':
      patch((r) => ({
        ...r,
        status: r.status === 'error' ? 'error' : 'done',
        meta: {
          iterations: event.iterations,
          toolCalls: event.tool_call_count,
          modelId: event.model_id,
        },
      }))
      break
  }
}

function EmptyState({ onPick }: { onPick: (q: string) => void }) {
  const { t } = useTranslation()
  return (
    <div className="agent-empty">
      <span className="agent-empty-mark" aria-hidden="true">
        <Bot size={30} strokeWidth={1.4} />
      </span>
      <h3>{t('agent.empty.title')}</h3>
      <p>{t('agent.empty.body')}</p>
      <div className="agent-suggestions">
        {SUGGESTIONS_KEYS.map((key) => {
          const text = t(`agent.suggestions.${key}`)
          return (
            <button
              key={key}
              className="agent-suggestion"
              onClick={() => onPick(text)}
              type="button"
            >
              {text}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function RunView({ run }: { run: AgentRun }) {
  const { t } = useTranslation()
  return (
    <div className="agent-run">
      <article className="chat-bubble chat-bubble-user agent-query">
        <span className="chat-bubble-avatar" aria-hidden="true">
          <UserRound size={14} />
        </span>
        <div className="chat-bubble-body">
          <header>
            <strong>{t('agent.you')}</strong>
          </header>
          <p className="chat-bubble-text">{run.query}</p>
        </div>
      </article>

      {run.steps.length > 0 && (
        <div className="agent-trace">
          {run.steps.map((step, idx) => (
            <TraceStepView key={idx} step={step} />
          ))}
        </div>
      )}

      <article className="chat-bubble chat-bubble-assistant agent-answer">
        <span className="chat-bubble-avatar" aria-hidden="true">
          <Bot size={14} />
        </span>
        <div className="chat-bubble-body">
          <header>
            <strong>{t('agent.assistant')}</strong>
            {run.status === 'running' && (
              <span className="badge badge-pending agent-running">
                <Loader2 size={11} className="spin" /> {t('agent.thinking')}
              </span>
            )}
            {run.meta?.toolCalls != null && run.status !== 'running' && (
              <span className="agent-meta">
                {t('agent.meta', {
                  tools: run.meta.toolCalls,
                  iters: run.meta.iterations ?? 0,
                })}
              </span>
            )}
          </header>
          {run.answer ? (
            <div className="agent-answer-text">{run.answer}</div>
          ) : run.status === 'running' ? (
            <p className="chat-bubble-text empty">{t('agent.gathering')}</p>
          ) : null}
          {run.status === 'error' && (
            <div className="chat-composer-error">{run.error}</div>
          )}
        </div>
      </article>
    </div>
  )
}

function TraceStepView({ step }: { step: RunStep }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)

  if (step.kind === 'note') {
    return <p className="agent-note">{step.text}</p>
  }

  if (step.kind === 'tool_call') {
    return (
      <div className="agent-step agent-step-call">
        <span className="agent-step-icon" aria-hidden="true">
          <Wrench size={12} />
        </span>
        <span className="agent-step-tool">{toolLabel(t, step.tool)}</span>
        {step.arguments && Object.keys(step.arguments).length > 0 && (
          <code className="agent-step-args">{compactArgs(step.arguments)}</code>
        )}
      </div>
    )
  }

  // tool_result
  const summary = summarizeResult(step.result ?? {})
  return (
    <div className="agent-step agent-step-result">
      <button
        className="agent-step-result-toggle"
        onClick={() => setOpen((v) => !v)}
        type="button"
        aria-expanded={open}
      >
        <span className="agent-step-result-summary">{summary}</span>
        {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
      </button>
      {open && (
        <pre className="agent-step-result-json">
          {JSON.stringify(step.result, null, 2)}
        </pre>
      )}
    </div>
  )
}

function toolLabel(t: (k: string) => string, tool?: string): string {
  if (!tool) return ''
  const key = `agent.tools.${tool}`
  const label = t(key)
  // i18n returns the key itself when missing; fall back to the raw name.
  return label === key ? tool : label
}

function compactArgs(args: ToolCallArgs): string {
  const entries = Object.entries(args)
  if (entries.length === 0) return ''
  return entries
    .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
    .join(', ')
}

/**
 * One-line summary of a tool result so the collapsed row is informative
 * without expanding the raw JSON. Prefers common count fields.
 */
function summarizeResult(result: Record<string, unknown>): string {
  if (typeof result.error === 'string') return `⚠ ${result.error}`
  const countKeys = [
    'total',
    'member_count',
    'passage_count',
    'accessible_space_count',
  ]
  for (const key of countKeys) {
    if (typeof result[key] === 'number') {
      const name = result.unit_name || result.space_name || result.query
      return name ? `${result[key]} · ${String(name)}` : `${result[key]}`
    }
  }
  if (result.ambiguous === true) return '多个匹配 / multiple matches'
  const keys = Object.keys(result)
  return keys.length ? keys.slice(0, 3).join(', ') : '—'
}

function Composer({
  value,
  onChange,
  onSubmit,
  busy,
}: {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
  busy: boolean
}) {
  const { t } = useTranslation()
  const ref = useRef<HTMLTextAreaElement>(null)
  const canSend = value.trim().length > 0 && !busy

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault()
      if (canSend) onSubmit()
    }
  }

  return (
    <form
      className="chat-composer agent-composer"
      onSubmit={(event) => {
        event.preventDefault()
        if (canSend) onSubmit()
      }}
    >
      <textarea
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={t('agent.composerPlaceholder')}
        ref={ref}
        rows={2}
        value={value}
      />
      <button className="primary-button" disabled={!canSend} type="submit">
        {busy ? <Loader2 size={14} className="spin" /> : <Send size={14} />}
        <span>{t('agent.run')}</span>
      </button>
    </form>
  )
}
