import {
  ArrowUp,
  BookOpenText,
  Bot,
  Check,
  ChevronDown,
  ChevronUp,
  Cpu,
  FileText,
  Hash,
  ImagePlus,
  Images,
  KeyRound,
  Layers,
  MessageSquarePlus,
  Paperclip,
  Pencil,
  Flame,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  UserRound,
  Wrench,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { SpaceSummary } from '../spaces/types'

import { useAuth } from '../auth/AuthContext'
import { useAuthModal } from '../auth/AuthModalContext'
import { DocumentTextViewer } from '../documents/DocumentTextViewer'
import {
  getModelSelections,
  getUsage,
  listUsableChatModels,
  type UsableChatModel,
  type UsageStatus,
} from '../settings/api'
import { describeModel } from '../settings/modelDescriptions'
import { listAgents } from '../agent/api'
import type { AgentSummary } from '../agent/types'

import { MAX_IMAGE_QUERY_TEXT, MAX_QUERY_PICTURE_BYTES, fetchQueryPicture } from './api'
import { useChat } from './ChatContext'
import { ensureQueryPicture, useQueryPicture } from './queryPictures'
import { ImageMatchesRail } from './ImageMatches'
import { NewConversationDialog } from './NewConversationDialog'
import { ReviewCard } from './ReviewCard'
import { ScopeChipStrip } from './ScopeChipStrip'
import type {
  AgentTraceStep,
  ConversationSummary,
  MessageCitation,
  MessageContent,
  MessageSummary,
} from './types'
import { messageImageMatches, messageImageQuery, messageIsPictureAnswer } from './types'
import { useFormatDate } from '../../i18n/formatDate'
import { getGuestStarters } from './guestApi'
import {
  GUEST_STARTER_BATCH,
  orderStarters,
  pickStarters,
  type RankedStarter,
} from './starters'

/**
 * Chat panel — two-pane layout:
 *
 *   ┌─────────────┬──────────────────────────────┐
 *   │ anchor      │ header (title + refresh)     │
 *   │ picker      │  ------------------------    │
 *   │ conv list   │ messages (timeline)          │
 *   │ (all spaces)│  ------------------------    │
 *   │             │ composer (textarea + send)   │
 *   └─────────────┴──────────────────────────────┘
 *
 * The sidebar's conversation list is cross-space: every conversation
 * the caller can reach across their accessible spaces, sorted by most
 * recent activity. The anchor picker only decides which space **new**
 * conversations get created in; the "New" button opens a dialog that
 * lets the user override that anchor per thread.
 */
export function ChatPanel() {
  const { t } = useTranslation()
  const { anchorSpace, accessibleSpaces } = useChat()

  if (accessibleSpaces.length === 0) {
    return <NoSpaceState />
  }

  return (
    <section className="panel chat-panel">
      <div className="chat-panel-header">
        <div>
          <span className="section-label">
            <Sparkles size={15} /> {t('chat.title')}
          </span>
          <p>
            {anchorSpace
              ? t('chat.description', { space: anchorSpace.name })
              : t('chat.descriptionNoAnchor')}
          </p>
        </div>
      </div>
      <ThreadWorkspace />
    </section>
  )
}

/**
 * Owns the sources rail's "which message's citations are showing" and
 * "which one is momentarily highlighted" state, so a click on an
 * ``[N]`` marker inside the message timeline (``ThreadColumn``) and the
 * corresponding card in the rail (``CitationsRail``) can refer to the
 * same thing without threading state through ``ChatContext`` — it's
 * pure UI, scoped to one render of the workspace.
 */
function ThreadWorkspace() {
  const { messages, currentConversationId } = useChat()
  const [activeMessageId, setActiveMessageId] = useState<string | null>(null)
  const [highlightIndex, setHighlightIndex] = useState<number | null>(null)
  const railRef = useRef<HTMLDivElement>(null)
  const pictureRailRef = useRef<HTMLDivElement>(null)

  // Reset the pinned message whenever the thread itself changes — an
  // explicit click always wins within one conversation, but switching
  // conversations should fall back to "latest cited answer" again.
  useEffect(() => {
    setActiveMessageId(null)
    setHighlightIndex(null)
  }, [currentConversationId])

  // Answers that have something for the right-hand rail: cited passages or found pictures.
  const railMessages = useMemo(() => {
    if (messages.kind !== 'ready') return []
    return messages.items.filter(
      (m) =>
        messageCitations(m.content).length > 0 ||
        messageImageMatches(m.content).length > 0,
    )
  }, [messages])

  const activeMessage =
    railMessages.find((m) => m.id === activeMessageId) ??
    railMessages[railMessages.length - 1] ??
    null
  const citations = activeMessage ? messageCitations(activeMessage.content) : []
  const imageMatches = activeMessage ? messageImageMatches(activeMessage.content) : []
  const hasRail = citations.length > 0 || imageMatches.length > 0

  const jumpToCitation = useCallback((messageId: string, index: number) => {
    setActiveMessageId(messageId)
    setHighlightIndex(index)
    // Let the rail swap to the right message's cards before scrolling.
    requestAnimationFrame(() => {
      const el = railRef.current?.querySelector<HTMLElement>(
        `[data-citation-index="${index}"]`,
      )
      el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    })
    // Clear after the CSS transition has had time to draw attention —
    // re-clicking the same marker should re-trigger the flash.
    window.setTimeout(() => setHighlightIndex((cur) => (cur === index ? null : cur)), 1600)
  }, [])

  return (
    <div className={hasRail ? 'chat-workspace has-citations' : 'chat-workspace'}>
      <ConversationsColumn />
      <ThreadColumn onCiteClick={jumpToCitation} />
      {citations.length > 0 && imageMatches.length > 0 ? (
        // An ordinary answer with pictures that might be what was asked for: both, stacked.
        <div className="chat-rail-stack">
          <CitationsRail
            citations={citations}
            highlightIndex={highlightIndex}
            railRef={railRef}
          />
          <ImageMatchesRail matches={imageMatches} railRef={pictureRailRef} related />
        </div>
      ) : (
        <>
          {citations.length > 0 && (
            <CitationsRail
              citations={citations}
              highlightIndex={highlightIndex}
              railRef={railRef}
            />
          )}
          {citations.length === 0 && imageMatches.length > 0 && (
            <ImageMatchesRail
              matches={imageMatches}
              railRef={pictureRailRef}
              related={activeMessage ? !messageIsPictureAnswer(activeMessage.content) : false}
            />
          )}
        </>
      )}
    </div>
  )
}

function NoSpaceState() {
  const { t } = useTranslation()
  return (
    <section className="panel">
      <div className="state-panel">{t('chat.noSpace')}</div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Left column: anchor picker + cross-space conversations list
// ---------------------------------------------------------------------------

function ConversationsColumn() {
  const { t } = useTranslation()
  const {
    anchorSpace,
    accessibleSpaces,
    conversations,
    currentConversationId,
    selectConversation,
    reloadConversations,
    createNewConversation,
  } = useChat()
  const { isGuest } = useAuth()
  const { openAuth } = useAuthModal()
  const [newOpen, setNewOpen] = useState(false)
  const [creating, setCreating] = useState(false)

  // Fast lookup for the per-row anchor pill: space_id → space summary.
  const spaceById = useMemo(() => {
    const m = new Map<string, SpaceSummary>()
    for (const s of accessibleSpaces) m.set(s.id, s)
    return m
  }, [accessibleSpaces])

  // A new conversation is filed under the personal space by default. Only
  // ask which space to file it under when the user actually has more than
  // one (e.g. team members with shared spaces).
  const startNew = async () => {
    if (isGuest) {
      openAuth()
      return
    }
    if (accessibleSpaces.length > 1) {
      setNewOpen(true)
      return
    }
    setCreating(true)
    try {
      await createNewConversation()
    } catch {
      /* surfaced via the composer error / list state */
    } finally {
      setCreating(false)
    }
  }

  return (
    <aside className="chat-sidebar">
      <div className="chat-sidebar-actions">
        <button
          className="primary-button chat-new-button"
          disabled={(!anchorSpace || creating) && !isGuest}
          onClick={() => void startNew()}
          type="button"
        >
          <MessageSquarePlus size={15} />
          <span>{t('chat.new')}</span>
        </button>
        <button
          className="icon-button chat-sidebar-refresh"
          disabled={conversations.kind === 'loading'}
          onClick={() => void reloadConversations()}
          type="button"
          aria-label={t('chat.refresh')}
        >
          <RefreshCw
            size={14}
            className={conversations.kind === 'loading' ? 'spin' : undefined}
          />
        </button>
      </div>
      {conversations.kind === 'loading' && (
        <div className="chat-sidebar-hint">{t('chat.loadingConversations')}</div>
      )}
      {conversations.kind === 'error' && (
        <div className="chat-sidebar-error">
          {t('chat.loadError')}: {conversations.message}
        </div>
      )}
      {conversations.kind === 'ready' && conversations.items.length === 0 && (
        <div className="chat-sidebar-hint">{t('chat.emptyConversations')}</div>
      )}
      {conversations.kind === 'ready' && conversations.items.length > 0 && (
        <ul className="chat-conversation-list">
          {conversations.items.map((conv) => (
            <ConversationListItem
              conversation={conv}
              isActive={conv.id === currentConversationId}
              key={conv.id}
              onSelect={() => selectConversation(conv.id)}
              space={spaceById.get(conv.space_id) ?? null}
            />
          ))}
        </ul>
      )}
      <NewConversationDialog open={newOpen} onClose={() => setNewOpen(false)} />
    </aside>
  )
}

function ConversationListItem({
  conversation,
  isActive,
  onSelect,
  space,
}: {
  conversation: ConversationSummary
  isActive: boolean
  onSelect: () => void
  space: SpaceSummary | null
}) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  return (
    <li>
      <button
        className={isActive ? 'chat-conversation-item active' : 'chat-conversation-item'}
        onClick={onSelect}
        type="button"
      >
        <strong>{conversation.title || t('chat.untitled')}</strong>
        <span className="chat-conversation-meta">
          {space && (
            <span
              className={`chip chip-inline chip-preset-${space.preset} chat-conversation-anchor`}
              title={space.slug}
            >
              <Layers size={9} />
              {space.name}
            </span>
          )}
          <small>
            {t('chat.messageCount', { count: conversation.message_count })}
            {conversation.last_message_at
              ? ` · ${formatDate(conversation.last_message_at)}`
              : ''}
          </small>
        </span>
      </button>
    </li>
  )
}

// ---------------------------------------------------------------------------
// Right column: message timeline + composer
// ---------------------------------------------------------------------------

function modelKey(m: { binding_id: string; model_id: string }): string {
  return `${m.binding_id}::${m.model_id}`
}

function ModelPicker() {
  const { t } = useTranslation()
  const { credentials, isGuest } = useAuth()
  const { currentConversation, currentConversationId, setConversationModel } = useChat()
  const [models, setModels] = useState<UsableChatModel[]>([])
  const [defaultKey, setDefaultKey] = useState<string>('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!credentials || isGuest) return
    let cancelled = false
    Promise.all([listUsableChatModels(credentials, true), getModelSelections(credentials)])
      .then(([u, s]) => {
        if (cancelled) return
        setModels(u.items)
        setDefaultKey(
          s.chat.model_id && s.chat.binding_id
            ? `${s.chat.binding_id}::${s.chat.model_id}`
            : '',
        )
      })
      .catch(() => {
        /* non-critical: fall back to "default" only */
      })
    return () => {
      cancelled = true
    }
  }, [credentials, isGuest])

  const [open, setOpen] = useState(false)
  const anchorRef = useRef<HTMLDivElement>(null)

  // Close the popover on outside click.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (anchorRef.current && !anchorRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  if (isGuest || !currentConversationId) return null

  const choice = currentConversation?.model_choice ?? null
  const value = choice ? `${choice.binding_id}::${choice.model_id}` : ''

  // Whether the org has explicitly saved a default chat model (an admin
  // picked one in Model Settings). When they haven't, there's nothing
  // meaningful to collapse into an unnamed "Default model" placeholder —
  // the platform's own model (e.g. DeepSeek) is just shown as a normal,
  // nameable option like any other.
  const hasOrgDefault = defaultKey !== ''
  const defaultName = models.find((m) => modelKey(m) === defaultKey)?.display_name

  // The model actually in effect when the org has no explicit default —
  // whichever usable model is flagged as "the" platform default (see
  // ``UsableChatModel.is_default`` — exactly one platform model carries
  // this now that several platform vendors can coexist).
  const platformDefault = models.find((m) => m.is_default) ?? models.find((m) => m.is_platform_default)
  const platformDefaultKey = platformDefault ? modelKey(platformDefault) : ''

  // The "default" placeholder already represents the org-selected model
  // (or, lacking one, the platform default), so never list that same
  // entry a second time as a plain choice.
  const collapsedKey = hasOrgDefault ? defaultKey : platformDefaultKey
  const options = models.filter((m) => modelKey(m) !== collapsedKey)

  // Group the remaining options: platform models bucketed by vendor (in
  // first-seen order), then the caller's own private BYOK models as a
  // separate trailing group. Org-owned shared bindings (vendor-less,
  // is_platform_default=false, is_private=false) fall into a bucket of
  // their own vendor key (or "" if unset) alongside platform ones would
  // be confusing, so they get their own group first.
  const orgOwned = options.filter((m) => !m.is_platform_default && !m.is_private)
  const platformOptions = options.filter((m) => m.is_platform_default)
  const privateOptions = options.filter((m) => m.is_private)

  const platformGroups: { vendor: string; items: UsableChatModel[] }[] = []
  for (const m of platformOptions) {
    const vendorKey = m.vendor ?? ''
    let group = platformGroups.find((g) => g.vendor === vendorKey)
    if (!group) {
      group = { vendor: vendorKey, items: [] }
      platformGroups.push(group)
    }
    group.items.push(m)
  }

  const current = value ? models.find((m) => modelKey(m) === value) : null

  const triggerLabel = current
    ? current.display_name
    : hasOrgDefault
      ? defaultName
        ? t('chat.model.defaultNamed', { name: defaultName })
        : t('chat.model.default')
      : platformDefault?.display_name ?? t('chat.model.default')

  const choose = async (v: string) => {
    setBusy(true)
    try {
      // Picking the model that's already in effect without an override
      // is the same as clearing the override — don't write a redundant
      // explicit one.
      const effective = !hasOrgDefault && v === platformDefaultKey ? '' : v
      if (!effective) {
        await setConversationModel(null, null)
      } else {
        const i = effective.indexOf('::')
        await setConversationModel(effective.slice(i + 2), effective.slice(0, i))
      }
      setOpen(false)
    } finally {
      setBusy(false)
    }
  }

  const renderOption = (m: UsableChatModel) => {
    const key = modelKey(m)
    const active = hasOrgDefault
      ? key === value
      : value === ''
        ? key === platformDefaultKey
        : key === value
    return (
      <button
        className={active ? 'chat-model-menu-item is-active' : 'chat-model-menu-item'}
        disabled={busy}
        key={key}
        onClick={() => void choose(key)}
        role="option"
        type="button"
      >
        <span className="chat-model-menu-main">
          <span className="chat-model-menu-name">
            {m.is_private && (
              <KeyRound className="chat-model-menu-private-icon" size={11} />
            )}
            {m.display_name}
            {!m.is_platform_default && (
              <span className="chat-model-menu-provider">{m.binding_name}</span>
            )}
            {m.is_default && (
              <span className="chat-model-menu-default-badge">
                {t('chat.model.defaultBadge')}
              </span>
            )}
          </span>
          <span className="chat-model-menu-desc">{describeModel(m.model_id, t)}</span>
        </span>
        {active && <Check className="chat-model-menu-check" size={15} />}
      </button>
    )
  }

  return (
    <div className="chat-tool-anchor" ref={anchorRef}>
      {open && (
        <div className="chat-model-menu" role="listbox">
          <div className="chat-model-menu-scroll">
            {hasOrgDefault && (
              <button
                className={value === '' ? 'chat-model-menu-item is-active' : 'chat-model-menu-item'}
                disabled={busy}
                onClick={() => void choose('')}
                role="option"
                type="button"
              >
                <span className="chat-model-menu-main">
                  <span className="chat-model-menu-name">
                    {t('chat.model.defaultOption')}
                  </span>
                  <span className="chat-model-menu-desc">
                    {defaultName
                      ? t('chat.model.defaultDescNamed', { name: defaultName })
                      : t('chat.model.defaultDesc')}
                  </span>
                </span>
                {value === '' && <Check className="chat-model-menu-check" size={15} />}
              </button>
            )}

            {orgOwned.length > 0 && (
              <>
                <div className="chat-model-menu-group-label">
                  {t('chat.model.group.org')}
                </div>
                {orgOwned.map(renderOption)}
              </>
            )}

            {platformGroups.length > 0 && (
              <div className="chat-model-menu-group-label">
                {t('chat.model.group.platform')}
              </div>
            )}
            {platformGroups.map((g) => (
              <div className="chat-model-menu-vendor-group" key={g.vendor || '__none'}>
                {g.vendor && (
                  <div className="chat-model-menu-vendor-label">
                    {vendorDisplayName(g.vendor)}
                  </div>
                )}
                {g.items.map(renderOption)}
              </div>
            ))}

            {privateOptions.length > 0 && (
              <>
                <div className="chat-model-menu-group-label">
                  <KeyRound size={11} /> {t('chat.model.group.private')}
                </div>
                {privateOptions.map(renderOption)}
              </>
            )}
          </div>
        </div>
      )}
      <button
        className="chat-tool-button chat-model-trigger"
        disabled={busy}
        onClick={() => setOpen((o) => !o)}
        title={t('chat.model.label')}
        type="button"
      >
        <Cpu size={13} />
        <span className="chat-model-trigger-label">{triggerLabel}</span>
        <ChevronDown size={12} />
      </button>
    </div>
  )
}

// Minimal vendor label mapping for the in-chat picker — mirrors the
// backend's VENDOR_DISPLAY_NAMES (services/ai/registry.py). Kept local
// rather than fetched so the menu never blocks on an extra request;
// falls back to the raw vendor string for anything unmapped (e.g. a
// vendor an admin just wired up through the platform console).
const VENDOR_DISPLAY_NAMES: Record<string, string> = {
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  dashscope: '通义千问 · DashScope',
  moonshot: 'Kimi · Moonshot',
  zhipu: '智谱 · GLM',
}

function vendorDisplayName(vendor: string): string {
  return VENDOR_DISPLAY_NAMES[vendor] ?? vendor
}

/**
 * Agent selector — a flat list of available agents (no type grouping;
 * builtin / external / future MCP are implementation details the user
 * shouldn't see). Picking one binds it to the conversation.
 */
function AgentPicker({
  agents,
  currentAgentId,
  onChoose,
}: {
  agents: AgentSummary[]
  currentAgentId: string | null
  onChoose: (agentId: string | null) => Promise<void>
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const anchorRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (anchorRef.current && !anchorRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const current = agents.find((a) => a.id === currentAgentId) ?? null
  const label = current ? current.name : t('chat.agent.pick')

  const choose = async (id: string | null) => {
    setBusy(true)
    try {
      await onChoose(id)
      setOpen(false)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="chat-tool-anchor" ref={anchorRef}>
      {open && (
        <div className="chat-model-menu" role="listbox">
          <div className="chat-model-menu-scroll">
            <button
              className={
                currentAgentId === null
                  ? 'chat-model-menu-item is-active'
                  : 'chat-model-menu-item'
              }
              disabled={busy}
              onClick={() => void choose(null)}
              role="option"
              type="button"
            >
              <span className="chat-model-menu-main">
                <span className="chat-model-menu-name">{t('chat.agent.none')}</span>
                <span className="chat-model-menu-desc">{t('chat.agent.noneDesc')}</span>
              </span>
              {currentAgentId === null && (
                <Check className="chat-model-menu-check" size={15} />
              )}
            </button>
            {agents.map((a) => {
              const active = a.id === currentAgentId
              return (
                <button
                  className={
                    active ? 'chat-model-menu-item is-active' : 'chat-model-menu-item'
                  }
                  disabled={busy}
                  key={a.id}
                  onClick={() => void choose(a.id)}
                  role="option"
                  type="button"
                >
                  <span className="chat-model-menu-main">
                    <span className="chat-model-menu-name">{a.name}</span>
                    {a.description && (
                      <span className="chat-model-menu-desc">{a.description}</span>
                    )}
                  </span>
                  {active && <Check className="chat-model-menu-check" size={15} />}
                </button>
              )
            })}
            {agents.length === 0 && (
              <p className="chat-mention-hint" style={{ padding: '8px 10px' }}>
                {t('chat.agent.empty')}
              </p>
            )}
          </div>
        </div>
      )}
      <button
        className={
          current
            ? 'chat-tool-button chat-model-trigger is-active'
            : 'chat-tool-button chat-model-trigger'
        }
        onClick={() => setOpen((o) => !o)}
        title={t('chat.agent.label')}
        type="button"
      >
        <Bot size={14} />
        <span className="chat-model-trigger-label">{label}</span>
        <ChevronDown size={12} />
      </button>
    </div>
  )
}

function EditableTitle({
  conversationId,
  title,
  messageCount,
}: {
  conversationId: string
  title: string
  messageCount: number
}) {
  const { t } = useTranslation()
  const { renameConversation } = useChat()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(title)
  const [busy, setBusy] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const begin = () => {
    setDraft(title)
    setEditing(true)
  }

  useEffect(() => {
    if (editing) inputRef.current?.select()
  }, [editing])

  const commit = async () => {
    const next = draft.trim()
    if (!next || next === title) {
      setEditing(false)
      return
    }
    setBusy(true)
    try {
      await renameConversation(conversationId, next)
      setEditing(false)
    } catch {
      /* keep editing on failure */
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="chat-thread-title">
      {editing ? (
        <input
          className="chat-thread-title-input"
          disabled={busy}
          onBlur={() => void commit()}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              void commit()
            } else if (e.key === 'Escape') {
              setEditing(false)
            }
          }}
          maxLength={200}
          ref={inputRef}
          value={draft}
        />
      ) : (
        <button
          className="chat-thread-title-button"
          onClick={begin}
          title={t('chat.rename')}
          type="button"
        >
          <strong>{title || t('chat.untitled')}</strong>
          <Pencil className="chat-thread-title-edit" size={13} />
        </button>
      )}
      <small>{t('chat.messageCount', { count: messageCount })}</small>
    </div>
  )
}

function ThreadColumn({
  onCiteClick,
}: {
  onCiteClick: (messageId: string, index: number) => void
}) {
  const { t } = useTranslation()
  const {
    currentConversation,
    currentConversationId,
    messages,
    sending,
    pendingReview,
    reviewError,
    approveReview,
    cancelReview,
    previewReview,
  } = useChat()
  const { isGuest } = useAuth()

  if (!currentConversationId) {
    return (
      <section className="chat-thread">
        <div className="state-panel">{t('chat.pickOrCreate')}</div>
      </section>
    )
  }

  return (
    <section className="chat-thread">
      <header className="chat-thread-header">
        <div className="chat-thread-header-top">
          <EditableTitle
            conversationId={currentConversationId}
            title={currentConversation?.title ?? ''}
            messageCount={currentConversation?.message_count ?? 0}
          />
        </div>
        <ScopeChipStrip />
      </header>
      <div className="chat-messages">
        {messages.kind === 'loading' && (
          <div className="state-panel">{t('chat.loadingMessages')}</div>
        )}
        {messages.kind === 'error' && (
          <div className="state-panel error-state">
            {t('chat.loadError')}: {messages.message}
          </div>
        )}
        {messages.kind === 'ready' && messages.items.length === 0 && (
          <div className="chat-empty-thread">{t('chat.emptyMessages')}</div>
        )}
        {messages.kind === 'ready' &&
          messages.items.map((m) => (
            <MessageBubble key={m.id} message={m} onCiteClick={onCiteClick} />
          ))}
        {pendingReview && pendingReview.conversationId === currentConversationId && (
          <ReviewCard
            // A fresh card for each held prompt, so its edits start clean.
            key={pendingReview.review.pending_id}
            busy={sending.active}
            error={reviewError}
            onApprove={(edits) => void approveReview(edits)}
            onCancel={() => void cancelReview()}
            onPreview={previewReview}
            review={pendingReview.review}
          />
        )}
      </div>
      {isGuest && <GuestStarters />}
      <Composer />
      {sending.error && (
        <div className="chat-composer-error">{sending.error}</div>
      )}
    </section>
  )
}

/**
 * Suggested questions for a visitor exploring Quberi.
 *
 * - Ordered by how often guests have asked them (the server ranks; with no
 *   data yet it is the curated order). The most-asked carry a flame badge.
 * - A batch of :data:`GUEST_STARTER_BATCH` is on screen. Asking one removes it
 *   and the next slides up, so the strip stays full; "next batch" pages
 *   through the rest without having to ask anything first.
 * - Once a conversation exists the strip shrinks to one scrolling row so it
 *   doesn't compete with the answers. It goes away only when the day's
 *   questions are used up or every suggestion has been asked.
 */
function GuestStarters() {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const { messages, sendUserMessage, sending, guestQuota } = useChat()
  const [ranked, setRanked] = useState<RankedStarter[] | null>(null)
  const [batch, setBatch] = useState(0)

  // Popularity order from the server. On failure we keep the curated order,
  // so the suggestions never disappear because a request failed.
  useEffect(() => {
    if (!credentials?.isGuest) return
    let cancelled = false
    getGuestStarters(credentials)
      .then((items) => {
        if (!cancelled) setRanked(items.map((i) => ({ key: i.key, hot: i.hot })))
      })
      .catch(() => {
        /* non-critical */
      })
    return () => {
      cancelled = true
    }
  }, [credentials])

  if (guestQuota?.remaining === 0) return null

  const asked = new Set<string>()
  if (messages.kind === 'ready') {
    for (const m of messages.items) {
      if (m.role === 'user' && (m.content as { guestLocal?: unknown }).guestLocal === true) {
        asked.add(messageText(m.content).trim())
      }
    }
  }
  const remaining = orderStarters(ranked)
    .map((r) => ({ ...r, text: t(`chat.guest.starters.${r.key}`) }))
    .filter((r) => !asked.has(r.text))
  if (remaining.length === 0) return null

  const shown = pickStarters(remaining, batch)
  const compact = asked.size > 0
  return (
    <div
      aria-label={t('chat.guest.startersLabel')}
      className={compact ? 'chat-starters chat-starters-compact' : 'chat-starters'}
      role="group"
    >
      <span className="chat-starters-label">
        {t(compact ? 'chat.guest.startersMore' : 'chat.guest.startersLabel')}
      </span>
      {shown.map((q) => (
        <button
          className={q.hot ? 'chat-starter chat-starter-hot' : 'chat-starter'}
          disabled={sending.active}
          key={q.key}
          onClick={() => void sendUserMessage(q.text, { starter: q.key })}
          title={q.hot ? t('chat.guest.hot') : undefined}
          type="button"
        >
          {q.hot && (
            <Flame
              aria-label={t('chat.guest.hot')}
              className="chat-starter-flame"
              role="img"
              size={13}
            />
          )}
          {q.text}
        </button>
      ))}
      {remaining.length > GUEST_STARTER_BATCH && (
        <button
          className="chat-starter chat-starter-refresh"
          onClick={() => setBatch((b) => b + 1)}
          type="button"
        >
          <RefreshCw size={12} />
          {t('chat.guest.startersRefresh')}
        </button>
      )}
    </div>
  )
}

function messageText(content: MessageContent): string {
  const text = (content as { text?: unknown }).text
  return typeof text === 'string' ? text : ''
}

function messageCitations(content: MessageContent): MessageCitation[] {
  const raw = (content as { citations?: unknown }).citations
  return Array.isArray(raw) ? (raw as MessageCitation[]) : []
}

function messageTrace(content: MessageContent): AgentTraceStep[] {
  const meta = (content as { meta?: { trace?: unknown } }).meta
  const raw = meta?.trace
  return Array.isArray(raw) ? (raw as AgentTraceStep[]) : []
}

export function MessageBubble({
  message,
  onCiteClick,
}: {
  message: MessageSummary
  onCiteClick: (messageId: string, index: number) => void
}) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const isUser = message.role === 'user'
  const placeholder =
    (message.content as { placeholder?: unknown }).placeholder === true
  const citations = messageCitations(message.content)
  const foundPictures = messageImageMatches(message.content).length
  // Pictures that only ride along with a text answer are offered, not announced as the answer.
  const picturesAreAnswer = messageIsPictureAnswer(message.content)
  const pictureQuery = isUser ? messageImageQuery(message.content) : null
  // The picture the question was searched with: put here when it was just sent,
  // fetched from the server (which keeps it for its sender) when it is shown later.
  const queryPictureUrl = useQueryPicture(message.id)
  const { credentials } = useAuth()
  const keptOnServer = pictureQuery?.stored === true
  useEffect(() => {
    if (!keptOnServer || !credentials || credentials.isGuest) return
    void ensureQueryPicture(message.id, () =>
      fetchQueryPicture(credentials, message.conversation_id, message.id),
    )
  }, [keptOnServer, credentials, message.id, message.conversation_id])
  const trace = messageTrace(message.content)
  const reviewMeta = (
    message.content as {
      meta?: { review?: { cancelled?: boolean; edited_parts?: number; excluded_references?: number } }
    }
  ).meta?.review
  const cancelled = reviewMeta?.cancelled === true
  const editedBeforeSend =
    (reviewMeta?.edited_parts ?? 0) > 0 || (reviewMeta?.excluded_references ?? 0) > 0
  // Shown in the reader's language rather than the stored bilingual fallback.
  const text = cancelled ? t('chat.review.cancelledNote') : messageText(message.content)
  const streaming = message.status === 'pending'

  return (
    <article
      className={
        isUser ? 'chat-bubble chat-bubble-user' : 'chat-bubble chat-bubble-assistant'
      }
    >
      <span className="chat-bubble-avatar" aria-hidden="true">
        {isUser ? <UserRound size={14} /> : <Bot size={14} />}
      </span>
      <div className="chat-bubble-body">
        <header>
          <strong>{t(`chat.role.${message.role}`)}</strong>
          {placeholder && message.status !== 'pending' && (
            <span className="badge badge-pending">{t('chat.placeholder')}</span>
          )}
          {message.status === 'pending' && (
            <span className="badge badge-pending chat-bubble-streaming">
              {t('chat.streaming')}
            </span>
          )}
          {editedBeforeSend && (
            <span
              className="badge badge-pending"
              title={t('chat.review.editedBadgeHint')}
            >
              {t('chat.review.edited_badge')}
            </span>
          )}
          <time>{formatDate(message.created_at, 'time')}</time>
        </header>
        {!isUser && trace.length > 0 && (
          <AgentTracePanel trace={trace} defaultOpen={streaming && !text} />
        )}
        {pictureQuery && (
          <p className="chat-picture-query">
            <Images size={13} aria-hidden="true" />
            {pictureQuery.picture
              ? pictureQuery.stored
                ? t('chat.imageSearch.askedWithPictureKept')
                : queryPictureUrl
                  ? t('chat.imageSearch.askedWithPictureShown')
                  : t('chat.imageSearch.askedWithPicture')
              : t('chat.imageSearch.askedWithWords')}
          </p>
        )}
        {pictureQuery?.picture && queryPictureUrl && (
          <img
            alt={t('chat.imageSearch.queryPictureAlt')}
            className="chat-query-picture"
            src={queryPictureUrl}
          />
        )}
        {(text || !pictureQuery) && (
          <AnswerText
            text={text}
            citationCount={citations.length}
            onCiteClick={
              citations.length > 0
                ? (index) => onCiteClick(message.id, index)
                : undefined
            }
          />
        )}
        {!isUser && foundPictures > 0 && (
          <button
            className="link-button chat-show-pictures"
            onClick={() => onCiteClick(message.id, -1)}
            type="button"
          >
            <Images size={13} aria-hidden="true" />
            {picturesAreAnswer
              ? t('chat.imageSearch.showPictures', { count: foundPictures })
              : t('chat.imageSearch.showRelated', { count: foundPictures })}
          </button>
        )}
      </div>
    </article>
  )
}

/** Collapsible "reasoning" panel showing an agent's tool calls / notes. */
function AgentTracePanel({
  trace,
  defaultOpen,
}: {
  trace: AgentTraceStep[]
  defaultOpen: boolean
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(defaultOpen)
  // Only surface the meaningful steps (skip the final "answer" echo).
  const steps = trace.filter((s) => s.kind !== 'answer')
  if (steps.length === 0) return null

  const summarise = (v: unknown): string => {
    if (v == null) return ''
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    return s.length > 180 ? `${s.slice(0, 180)}…` : s
  }

  return (
    <div className="chat-trace">
      <button
        aria-expanded={open}
        className="chat-trace-toggle"
        onClick={() => setOpen((v) => !v)}
        type="button"
      >
        <Wrench size={13} />
        <span>{t('chat.trace.header', { count: steps.length })}</span>
        {open ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
      </button>
      {open && (
        <ul className="chat-trace-list">
          {steps.map((s, i) => (
            <li className="chat-trace-step" key={i}>
              {s.kind === 'note' ? (
                <>
                  <span className="chat-trace-label">{t('chat.trace.note')}</span>
                  <span className="chat-trace-text">{summarise(s.text)}</span>
                </>
              ) : s.kind === 'tool_result' ? (
                <>
                  <span className="chat-trace-label">
                    {t('chat.trace.toolResult')}
                    {s.tool ? ` · ${s.tool}` : ''}
                  </span>
                  <span className="chat-trace-text">{summarise(s.result)}</span>
                </>
              ) : (
                <>
                  <span className="chat-trace-label">
                    {t('chat.trace.toolCall')}
                    {s.tool ? ` · ${s.tool}` : ''}
                  </span>
                  {s.arguments != null && (
                    <span className="chat-trace-text">{summarise(s.arguments)}</span>
                  )}
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * Answer body renderer with clickable citation markers.
 *
 * Any ``[N]`` in the text is rendered as a clickable superscript chip —
 * clicking it jumps to and flashes the matching card in the sources
 * rail (``CitationsRail``), instead of just visually standing out.
 * Everything else stays plain text — full Markdown rendering is
 * deferred to a later slice.
 */
function AnswerText({
  text,
  citationCount,
  onCiteClick,
}: {
  text: string
  citationCount: number
  onCiteClick?: (index: number) => void
}) {
  if (!text) return <p className="chat-bubble-text empty"> </p>
  const parts = text.split(/(\[\d{1,3}\])/g)
  return (
    <p className="chat-bubble-text">
      {parts.map((part, idx) => {
        const marker = /^\[(\d{1,3})\]$/.exec(part)
        if (marker) {
          const n = Number(marker[1])
          // Only markers that resolve to a real citation in this
          // message are clickable — the model occasionally emits a
          // stray [N] past the reference list.
          if (onCiteClick && n >= 1 && n <= citationCount) {
            return (
              <sup key={idx}>
                <button
                  className="chat-citation-marker chat-citation-marker-link"
                  onClick={() => onCiteClick(n)}
                  type="button"
                >
                  [{n}]
                </button>
              </sup>
            )
          }
          return (
            <sup key={idx} className="chat-citation-marker">
              [{marker[1]}]
            </sup>
          )
        }
        return <span key={idx}>{part}</span>
      })}
    </p>
  )
}

/**
 * Right-hand sources rail. Stays mounted (and the same height as the
 * conversation) alongside the thread for as long as the active message
 * carries citations — see ``ThreadWorkspace``.
 */
function CitationsRail({
  citations,
  highlightIndex,
  railRef,
}: {
  citations: MessageCitation[]
  highlightIndex: number | null
  railRef: React.RefObject<HTMLDivElement | null>
}) {
  const { t } = useTranslation()
  const [viewerCitation, setViewerCitation] = useState<MessageCitation | null>(null)
  const usedCount = citations.filter((c) => c.used).length

  return (
    <aside className="chat-citations-rail" ref={railRef}>
      <header className="chat-citations-rail-head">
        <BookOpenText size={14} />
        <span>{t('chat.citations.panelTitle')}</span>
        <small>
          {t('chat.citations.header', {
            // i18next picks the _one/_other form from ``count``; without it
            // the key is not found and the raw key name is shown.
            count: citations.length,
            used: usedCount,
            total: citations.length,
          })}
        </small>
      </header>
      <ul className="chat-citations-rail-list">
        {citations.map((c) => (
          <li
            className={
              c.index === highlightIndex
                ? 'chat-citation-card flash'
                : c.used
                  ? 'chat-citation-card used'
                  : 'chat-citation-card'
            }
            data-citation-index={c.index}
            key={c.index}
          >
            <button
              className="chat-citation-card-button"
              onClick={() => setViewerCitation(c)}
              type="button"
            >
              <span className="chat-citation-card-head">
                <span className="chat-citation-index">[{c.index}]</span>
                <span className="chat-citation-doc-name" title={c.document_name}>
                  <FileText size={11} />
                  {c.document_name || t('documents.unknownUploader')}
                </span>
              </span>
              <small className="chat-citation-meta">
                <span>{t('chat.citations.chunk', { index: c.chunk_index })}</span>
                <span className="dot-sep">·</span>
                <span>{t('chat.citations.score', { score: c.score.toFixed(3) })}</span>
                {c.used && (
                  <span className="chat-citation-used-badge">
                    {t('chat.citations.usedBadge')}
                  </span>
                )}
              </small>
              <p className="chat-citation-preview">{trimPreview(c.text)}</p>
              <span className="chat-citation-open-hint">
                {t('chat.citations.openDocument')}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {viewerCitation && (
        <DocumentTextViewer
          spaceId={viewerCitation.space_id}
          documentId={viewerCitation.document_id}
          documentName={viewerCitation.document_name}
          charStart={viewerCitation.char_start}
          charEnd={viewerCitation.char_end}
          onClose={() => setViewerCitation(null)}
        />
      )}
    </aside>
  )
}

function trimPreview(text: string, limit = 220): string {
  const clean = text.replace(/\s+/g, ' ').trim()
  if (clean.length <= limit) return clean
  return `${clean.slice(0, limit)}…`
}

export function Composer() {
  const { t } = useTranslation()
  const {
    sending,
    sendUserMessage,
    sendImageSearch,
    guestQuota,
    currentConversationId,
    currentConversation,
    accessibleSpaces,
    scopeSources,
    reloadScopeSources,
    setScopeMode,
    addScopeSource,
    removeScopeSource,
    setConversationAgent,
    reviewBeforeSend,
    setReviewBeforeSend,
    searchMode,
    setSearchMode,
    pendingReview,
  } = useChat()
  const { isGuest, credentials } = useAuth()
  const { openAuth } = useAuthModal()
  const [text, setText] = useState('')
  const [usage, setUsage] = useState<UsageStatus | null>(null)
  const [mention, setMention] = useState<'closed' | 'root' | 'space' | 'agent'>('closed')
  const [attachOpen, setAttachOpen] = useState(false)
  // Picture search: a picture staged for the next send, and/or "search by words" mode.
  const [picture, setPicture] = useState<File | null>(null)
  const [wordsOnly, setWordsOnly] = useState(false)
  const [pictureError, setPictureError] = useState<string | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const mentionRef = useRef<HTMLDivElement>(null)
  const attachRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const searchingPictures = picture !== null || wordsOnly
  // "Pictures only" is a standing choice; a staged picture or "by words" is for one send.
  // Either way the question goes to the picture search, which calls no model.
  const picturesOnly = searchMode === 'image'
  const routeToPictures = searchingPictures || picturesOnly

  // A small preview of the staged picture; released when it changes or goes.
  useEffect(() => {
    if (!picture) {
      setPreviewUrl(null)
      return
    }
    const url = URL.createObjectURL(picture)
    setPreviewUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [picture])

  /** Stage a picture for searching; refuses anything that is not a picture or is too big. */
  const stagePicture = (file: File | null | undefined) => {
    if (!file) return
    if (!file.type.startsWith('image/')) {
      setPictureError(t('chat.imageSearch.notPicture'))
      return
    }
    if (file.size > MAX_QUERY_PICTURE_BYTES) {
      setPictureError(
        t('chat.imageSearch.tooBig', { mb: Math.round(MAX_QUERY_PICTURE_BYTES / 1024 / 1024) }),
      )
      return
    }
    setPictureError(null)
    setPicture(file)
    setAttachOpen(false)
    textareaRef.current?.focus()
  }

  const clearPictureSearch = () => {
    setPicture(null)
    setWordsOnly(false)
    setPictureError(null)
  }

  const refreshUsage = useCallback(async () => {
    if (!credentials || isGuest) return
    try {
      setUsage(await getUsage(credentials))
    } catch {
      /* non-critical; hide the hint on failure */
    }
  }, [credentials, isGuest])

  useEffect(() => {
    void refreshUsage()
  }, [refreshUsage])

  // Reset the composer whenever we switch conversation.
  useEffect(() => {
    setText('')
    setMention('closed')
    setPicture(null)
    setWordsOnly(false)
    setPictureError(null)
    textareaRef.current?.focus()
  }, [currentConversationId])

  // Load current scope sources when the space picker opens.
  useEffect(() => {
    if (mention === 'space') void reloadScopeSources()
  }, [mention, reloadScopeSources])

  // Load the usable agents once (for the "#" agent menu + the header chip).
  useEffect(() => {
    if (!credentials || isGuest) return
    let cancelled = false
    listAgents(credentials)
      .then((res) => {
        if (!cancelled) setAgents(res.items)
      })
      .catch(() => {
        /* non-critical: the agent menu just shows empty */
      })
    return () => {
      cancelled = true
    }
  }, [credentials, isGuest])

  // Close the toolbar popovers on outside click.
  useEffect(() => {
    if (mention === 'closed' && !attachOpen) return
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (mention !== 'closed' && mentionRef.current && !mentionRef.current.contains(target)) {
        setMention('closed')
      }
      if (attachOpen && attachRef.current && !attachRef.current.contains(target)) {
        setAttachOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [mention, attachOpen])

  const guestExhausted = isGuest && guestQuota !== null && guestQuota.remaining === 0
  // A held prompt must be decided before the next question; and an agent
  // conversation cannot be checked yet, so it is refused rather than skipped.
  const heldHere =
    pendingReview !== null && pendingReview.conversationId === currentConversationId
  const agentBlocksReview =
    reviewBeforeSend && !isGuest && Boolean(currentConversation?.agent_choice?.agent_id)
  // A picture search calls no model, so a held prompt or an agent does not get in its way.
  const canSend = routeToPictures
    ? (picture !== null || text.trim().length > 0) && !sending.active
    : text.trim().length > 0 && !sending.active && !heldHere && !agentBlocksReview

  const submit = async () => {
    if (guestExhausted) {
      openAuth()
      return
    }
    if (!canSend) return
    const value = text.trim()
    if (routeToPictures) {
      // Keep the picture and words if the search fails, so it can be tried again.
      const searchedWithWords = picture !== null && value.length > 0
      const ok = await sendImageSearch(picture, value)
      if (ok) {
        setText('')
        clearPictureSearch()
        // Pictures and documents: the words that came with the picture are also asked as
        // an ordinary question. (A picture alone has nothing to look up in documents.)
        if (searchMode === 'both' && searchedWithWords && !heldHere && !agentBlocksReview) {
          await sendUserMessage(value)
          void refreshUsage()
        }
      }
      textareaRef.current?.focus()
      return
    }
    setText('')
    await sendUserMessage(value)
    void refreshUsage()
    textareaRef.current?.focus()
  }

  const handlePaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (isGuest) return
    const file = Array.from(event.clipboardData.files).find((f) => f.type.startsWith('image/'))
    if (!file) return // plain text paste goes through as usual
    event.preventDefault()
    stagePicture(file)
  }

  const carriesFiles = (event: React.DragEvent) =>
    !isGuest && Array.from(event.dataTransfer.types).includes('Files')

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // "#" opens the mention menu (spaces / agents) instead of typing.
    if (event.key === '#' && !isGuest) {
      event.preventDefault()
      setMention('root')
      return
    }
    if (event.key === 'Escape' && mention !== 'closed') {
      setMention('closed')
      return
    }
    // Plain Enter sends; Shift+Enter keeps a newline.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void submit()
    }
  }

  const scopeMode = currentConversation?.scope_mode ?? 'auto'
  const sourceIds =
    scopeSources.kind === 'ready'
      ? new Set(scopeSources.items.map((s) => s.space_id))
      : new Set<string>()
  // In ``auto`` mode every accessible space is searched, so every box is
  // ticked. In ``custom`` mode only the explicitly-selected spaces are.
  const inScope = (id: string) => (scopeMode === 'auto' ? true : sourceIds.has(id))

  const toggleSpace = async (spaceId: string) => {
    if (scopeMode !== 'custom') {
      // Leaving auto: the server seeds the custom set with every accessible
      // space, so this first click means "deselect this one". Pull the
      // seeded rows into local state, then drop the toggled space.
      await setScopeMode('custom')
      await reloadScopeSources()
      await removeScopeSource(spaceId)
      return
    }
    if (sourceIds.has(spaceId)) {
      await removeScopeSource(spaceId)
    } else {
      await addScopeSource(spaceId)
    }
  }

  const currentAgentId = currentConversation?.agent_choice?.agent_id ?? null
  const currentAgent = agents.find((a) => a.id === currentAgentId) ?? null

  const chooseAgent = async (agentId: string | null) => {
    await setConversationAgent(agentId)
    setMention('closed')
  }

  const mentionMenu = (
    <div className="chat-mention-menu">
      <div className="chat-mention-head">
        <span>{t('chat.mention.title')}</span>
        <button
          aria-label={t('chat.mention.close')}
          className="dialog-close"
          onClick={() => setMention('closed')}
          type="button"
        >
          ×
        </button>
      </div>
      {mention === 'root' && (
        <div className="chat-mention-root">
          <button onClick={() => setMention('space')} type="button">
            <Layers size={14} /> {t('chat.mention.space')}
          </button>
          <button onClick={() => setMention('agent')} type="button">
            <Bot size={14} /> {t('chat.mention.agent')}
          </button>
        </div>
      )}
      {mention === 'space' && (
        <div className="chat-mention-list">
          <p className="chat-mention-hint">{t('chat.mention.spaceHint')}</p>
          {accessibleSpaces.map((s) => (
            <label className="chat-mention-item" key={s.id}>
              <input
                checked={inScope(s.id)}
                onChange={() => void toggleSpace(s.id)}
                type="checkbox"
              />
              <span>
                {s.name}
                {s.preset === 'personal' && (
                  <small> · {t('chat.mention.personal')}</small>
                )}
              </span>
            </label>
          ))}
          {accessibleSpaces.length === 0 && (
            <p className="chat-mention-hint">{t('chat.mention.noOther')}</p>
          )}
          <div className="chat-mention-actions">
            <button
              className="link-button"
              onClick={() => setMention('root')}
              type="button"
            >
              {t('chat.mention.back')}
            </button>
            <button
              className="primary-button"
              onClick={() => setMention('closed')}
              type="button"
            >
              {t('chat.mention.done')}
            </button>
          </div>
        </div>
      )}
      {mention === 'agent' && (
        <div className="chat-mention-list">
          <p className="chat-mention-hint">{t('chat.mention.agentHint')}</p>
          <label className="chat-mention-item">
            <input
              checked={currentAgentId === null}
              name="chat-agent"
              onChange={() => void chooseAgent(null)}
              type="radio"
            />
            <span>{t('chat.mention.agentNone')}</span>
          </label>
          {agents.map((a) => (
            <label className="chat-mention-item" key={a.id}>
              <input
                checked={currentAgentId === a.id}
                name="chat-agent"
                onChange={() => void chooseAgent(a.id)}
                type="radio"
              />
              <span>
                {a.name}
                {a.description && <small> · {a.description}</small>}
              </span>
            </label>
          ))}
          {agents.length === 0 && (
            <p className="chat-mention-hint">{t('chat.mention.agentEmpty')}</p>
          )}
          <div className="chat-mention-actions">
            <button
              className="link-button"
              onClick={() => setMention('root')}
              type="button"
            >
              {t('chat.mention.back')}
            </button>
            <button
              className="primary-button"
              onClick={() => setMention('closed')}
              type="button"
            >
              {t('chat.mention.done')}
            </button>
          </div>
        </div>
      )}
    </div>
  )

  return (
    <div className="chat-composer-wrap">
      <form
        className={dragging ? 'chat-composer is-dragging' : 'chat-composer'}
        onDragLeave={(event) => {
          // Moving over a child fires leave on the form; only a real exit counts.
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false)
        }}
        onDragOver={(event) => {
          if (!carriesFiles(event)) return
          event.preventDefault()
          setDragging(true)
        }}
        onDrop={(event) => {
          if (!carriesFiles(event)) return
          event.preventDefault()
          setDragging(false)
          stagePicture(
            Array.from(event.dataTransfer.files).find((f) => f.type.startsWith('image/')) ??
              event.dataTransfer.files[0],
          )
        }}
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        {isGuest && guestQuota && guestQuota.limit > 0 && (
          <div
            className={
              guestQuota.remaining > 0 ? 'chat-quota-hint' : 'chat-quota-hint exhausted'
            }
          >
            {guestQuota.remaining > 0
              ? t('chat.guest.remaining', {
                  remaining: guestQuota.remaining,
                  limit: guestQuota.limit,
                })
              : t('chat.guest.exhausted')}{' '}
            <button className="link-button" onClick={openAuth} type="button">
              {t('chat.guest.signIn')}
            </button>
          </div>
        )}
        {usage?.metered && usage.limit > 0 && (
          <div
            className={
              usage.remaining > 0 ? 'chat-quota-hint' : 'chat-quota-hint exhausted'
            }
          >
            {usage.remaining > 0
              ? t('chat.quotaRemaining', {
                  remaining: usage.remaining,
                  limit: usage.limit,
                })
              : t('chat.quotaExhausted')}
          </div>
        )}
        {heldHere && <div className="chat-quota-hint">{t('chat.review.heldHint')}</div>}
        {agentBlocksReview && (
          <div className="chat-quota-hint exhausted">{t('chat.review.agentUnsupported')}</div>
        )}
        {searchingPictures && (
          <div className="chat-picture-stage">
            {previewUrl ? (
              <img alt="" className="chat-picture-stage-thumb" src={previewUrl} />
            ) : (
              <span className="chat-picture-stage-thumb chat-picture-stage-words">
                <Images size={16} />
              </span>
            )}
            <span className="chat-picture-stage-text">
              <strong>
                {picture ? picture.name : t('chat.imageSearch.byWordsTitle')}
              </strong>
              <small>
                {picture ? t('chat.imageSearch.notSaved') : t('chat.imageSearch.byWordsHint')}
              </small>
            </span>
            <button
              aria-label={t('chat.imageSearch.remove')}
              className="icon-button"
              onClick={clearPictureSearch}
              type="button"
            >
              <X size={14} />
            </button>
          </div>
        )}
        {picture !== null && text.trim().length === 0 && searchMode === 'both' && (
          <div className="chat-quota-hint">{t('chat.searchMode.pictureOnlyHint')}</div>
        )}
        {pictureError && <div className="chat-quota-hint exhausted">{pictureError}</div>}
        {dragging && (
          <div className="chat-drop-hint" aria-hidden="true">
            {t('chat.imageSearch.dropHint')}
          </div>
        )}
        <textarea
          maxLength={routeToPictures ? MAX_IMAGE_QUERY_TEXT : undefined}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={t(
            isGuest
              ? 'chat.guest.placeholder'
              : routeToPictures
                ? 'chat.imageSearch.placeholder'
                : 'chat.composerPlaceholderMention',
          )}
          ref={textareaRef}
          rows={2}
          value={text}
        />
        <div className="chat-composer-toolbar">
          <div className="chat-composer-tools">
            <div className="chat-tool-anchor" ref={mentionRef}>
              {mention !== 'closed' && mentionMenu}
              <button
                aria-label={t('chat.mention.title')}
                className={
                  mention !== 'closed'
                    ? 'chat-tool-button is-active'
                    : 'chat-tool-button'
                }
                disabled={isGuest}
                onClick={() => {
                  setAttachOpen(false)
                  setMention((m) => (m === 'closed' ? 'root' : 'closed'))
                }}
                title={t('chat.mention.title')}
                type="button"
              >
                <Hash size={15} />
              </button>
              {currentAgent && (
                <button
                  className="chat-tool-button is-active chat-agent-chip"
                  disabled={isGuest}
                  onClick={() => {
                    setAttachOpen(false)
                    setMention('agent')
                  }}
                  title={t('chat.mention.agentActive', { name: currentAgent.name })}
                  type="button"
                >
                  <Bot size={14} />
                  <span className="chat-model-trigger-label">{currentAgent.name}</span>
                </button>
              )}
            </div>
            <div className="chat-tool-anchor" ref={attachRef}>
              <input
                accept="image/*"
                aria-label={t('chat.imageSearch.pick')}
                className="sr-only"
                onChange={(event) => {
                  stagePicture(event.target.files?.[0])
                  event.target.value = '' // picking the same file again should still fire
                }}
                ref={fileInputRef}
                tabIndex={-1}
                type="file"
              />
              {attachOpen && (
                <div className="chat-mention-menu chat-attach-menu">
                  <button
                    className="chat-attach-item"
                    onClick={() => fileInputRef.current?.click()}
                    type="button"
                  >
                    <ImagePlus size={14} />
                    <span>
                      <strong>{t('chat.imageSearch.pick')}</strong>
                      <small>{t('chat.imageSearch.pickHint')}</small>
                    </span>
                  </button>
                  <button
                    className="chat-attach-item"
                    onClick={() => {
                      setWordsOnly(true)
                      setAttachOpen(false)
                      textareaRef.current?.focus()
                    }}
                    type="button"
                  >
                    <Images size={14} />
                    <span>
                      <strong>{t('chat.imageSearch.byWordsTitle')}</strong>
                      <small>{t('chat.imageSearch.byWordsMenuHint')}</small>
                    </span>
                  </button>
                </div>
              )}
              <button
                aria-label={t('chat.attach.label')}
                aria-expanded={attachOpen}
                className={searchingPictures ? 'chat-tool-button is-active' : 'chat-tool-button'}
                disabled={isGuest}
                onClick={() => {
                  setMention('closed')
                  setAttachOpen((o) => !o)
                }}
                title={t('chat.attach.label')}
                type="button"
              >
                <Paperclip size={15} />
              </button>
            </div>
            {!isGuest && (
              <button
                aria-label={t('chat.review.enable')}
                aria-pressed={reviewBeforeSend}
                className={
                  reviewBeforeSend
                    ? 'chat-tool-button chat-review-toggle is-active'
                    : 'chat-tool-button chat-review-toggle'
                }
                onClick={() => setReviewBeforeSend(!reviewBeforeSend)}
                title={t('chat.review.enableHint')}
                type="button"
              >
                <ShieldCheck size={15} />
                <span className="chat-model-trigger-label">{t('chat.review.enable')}</span>
              </button>
            )}
            {!isGuest && (
              <div
                aria-label={t('chat.searchMode.label')}
                className="chat-search-mode"
                role="radiogroup"
              >
                {(['text', 'image', 'both'] as const).map((mode) => {
                  // A staged picture is always searched as a picture, so "documents only" is off.
                  const blocked = mode === 'text' && picture !== null
                  return (
                    <button
                      aria-checked={searchMode === mode}
                      className={
                        searchMode === mode
                          ? 'chat-search-mode-item is-active'
                          : 'chat-search-mode-item'
                      }
                      disabled={blocked}
                      key={mode}
                      onClick={() => setSearchMode(mode)}
                      role="radio"
                      title={t(
                        blocked ? 'chat.searchMode.textBlocked' : `chat.searchMode.${mode}Hint`,
                      )}
                      type="button"
                    >
                      {t(`chat.searchMode.${mode}`)}
                    </button>
                  )
                })}
              </div>
            )}
            <ModelPicker />
          </div>
          <button
            aria-label={t('chat.send')}
            className="chat-send-button"
            disabled={!canSend && !guestExhausted}
            title={t('chat.sendHint')}
            type="submit"
          >
            <ArrowUp size={17} />
          </button>
        </div>
      </form>
    </div>
  )
}
