import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { MessageBubble } from './ChatPanel'
import type { MessageSummary } from './types'

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }))
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ credentials: { sessionToken: 'tok', isGuest: false } }),
}))
vi.mock('../../i18n/formatDate', () => ({ useFormatDate: () => () => '10:00' }))

function answer(text: string, citations = 0): MessageSummary {
  return {
    id: 'a1',
    conversation_id: 'c1',
    role: 'assistant',
    content: {
      type: 'text',
      text,
      citations: Array.from({ length: citations }, (_, i) => ({
        index: i + 1, document_id: `d${i}`, space_id: 's', document_name: `doc${i}.md`,
        chunk_index: 0, text: 'x', score: 0.5, used: true, char_start: 0, char_end: 1,
      })),
    },
    author_user_id: null,
    status: 'completed',
    correlation_id: null,
    created_at: '2026-01-01T00:00:00Z',
  } as unknown as MessageSummary
}

describe('answer text', () => {
  it('shows **bold** as bold, without the asterisks', () => {
    const { container } = render(
      <MessageBubble message={answer('孔距为 **242 ± 0.1 mm**，原为 **240**。')} onCiteClick={() => undefined} />,
    )
    const bold = Array.from(container.querySelectorAll('.chat-bubble-text strong')).map((n) => n.textContent)
    expect(bold).toContain('242 ± 0.1 mm')
    expect(bold).toContain('240')
    expect(container.textContent).not.toContain('*')
  })

  it('keeps citation markers clickable inside and outside bold', () => {
    const cited: number[] = []
    render(
      <MessageBubble
        message={answer('**要求 [1]** 与 [2]。', 2)}
        onCiteClick={(_id, index) => cited.push(index)}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '[1]' }))
    fireEvent.click(screen.getByRole('button', { name: '[2]' }))
    expect(cited).toEqual([1, 2])
  })

  it('leaves a lone ** and a ** that spans lines as typed', () => {
    const { container } = render(
      <MessageBubble message={answer('前 ** 后\n**换行\n行**')} onCiteClick={() => undefined} />,
    )
    expect(container.querySelectorAll('.chat-bubble-text strong')).toHaveLength(0)
    expect(container.textContent).toContain('**')
  })

  it('never turns answer text into HTML', () => {
    const { container } = render(
      <MessageBubble message={answer('**<img src=x onerror=alert(1)>** 与 <b>x</b>')} onCiteClick={() => undefined} />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('b')).toBeNull()
    expect(container.textContent).toContain('<b>x</b>')
  })
})
