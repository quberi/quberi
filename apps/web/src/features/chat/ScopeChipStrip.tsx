import { Compass, Layers } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { useSpaces } from '../spaces/SpacesContext'

import { useChat } from './ChatContext'

/**
 * Read-only summary of the knowledge sources a conversation will search.
 *
 * Selection now happens in the composer's "#" menu; this strip only
 * *displays* what is currently in scope:
 *
 *   ``auto`` mode:   "全部可访问 · N 空间"
 *   ``custom`` mode: anchor space + any attached source spaces
 */
export function ScopeChipStrip() {
  const { t } = useTranslation()
  const { state } = useSpaces()
  const { currentConversation, scopeSources } = useChat()

  if (!currentConversation) return null

  const accessibleSpaces = state.kind === 'ready' ? state.items : []
  const isAuto = currentConversation.scope_mode === 'auto'
  const sources = scopeSources.kind === 'ready' ? scopeSources.items : []

  return (
    <div className="chat-scope-strip">
      <span className="chat-scope-summary-label">
        {t('chat.scope.summaryLabel')}
      </span>
      {isAuto ? (
        <span className="chat-scope-chip chip-auto">
          <Compass size={12} />
          <span>{t('chat.scope.autoLabel', { count: accessibleSpaces.length })}</span>
        </span>
      ) : sources.length === 0 ? (
        <span className="chat-scope-chip">
          <span>{t('chat.scope.noneLabel')}</span>
        </span>
      ) : (
        sources.map((s) => (
          <span
            className="chat-scope-chip chip-source"
            key={s.id}
            title={s.space_slug}
          >
            <Layers size={12} />
            <span>{s.space_name}</span>
          </span>
        ))
      )}
    </div>
  )
}
