import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ScopeChipStrip } from './ScopeChipStrip'
import type { ScopeSource } from './types'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) =>
      vars ? `${key}:${JSON.stringify(vars)}` : key,
  }),
}))

vi.mock('../spaces/SpacesContext', () => ({
  useSpaces: () => ({ state: { kind: 'ready', items: [] } }),
}))

const chat = vi.hoisted(() => ({ sources: [] as ScopeSource[] }))
vi.mock('./ChatContext', () => ({
  useChat: () => ({
    currentConversation: { id: 'c1', scope_mode: 'custom' },
    scopeSources: { kind: 'ready', items: chat.sources },
  }),
}))

const source = (over: Partial<ScopeSource>): ScopeSource => ({
  id: 'l1',
  conversation_id: 'c1',
  space_id: 's1',
  space_name: 'Handbook',
  space_slug: 'handbook',
  added_by_user_id: null,
  created_at: '',
  ...over,
})

describe('ScopeChipStrip', () => {
  it('shows the name of a readable source and a placeholder for one that is closed', () => {
    chat.sources = [
      source({}),
      source({ id: 'l2', space_id: 's2', space_name: '', space_slug: '', hidden: true }),
    ]
    render(<ScopeChipStrip />)
    expect(screen.getByText('Handbook')).toBeInTheDocument()
    expect(screen.getByText('chat.scope.hiddenSource')).toBeInTheDocument()
    expect(screen.getByText('Handbook').parentElement).toHaveAttribute('title', 'handbook')
    expect(screen.getByText('chat.scope.hiddenSource').parentElement).not.toHaveAttribute('title')
  })
})
