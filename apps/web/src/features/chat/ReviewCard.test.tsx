import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ReviewEditsBody, ReviewPayload } from './api'
import { ReviewCard } from './ReviewCard'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

const Q = '请联系 13812345678 的同事'
const R1 = '财务王芳，邮箱 wang@example.com。'
const R2 = '第二段，没有敏感信息。'

function span(text: string, needle: string, kind: 'phone' | 'email') {
  const start = text.indexOf(needle)
  return { start, end: start + needle.length, kind }
}

function review(overrides: Partial<ReviewPayload> = {}): ReviewPayload {
  return {
    pending_id: 'p1',
    expires_in: 900,
    model: { model_id: 'm', display_name: 'Model M', vendor: 'acme' },
    question: { text: Q, spans: [span(Q, '13812345678', 'phone')] },
    history: [],
    references: [
      {
        index: 1, document_name: 'a.docx', chunk_index: 0, included: true, text: R1,
        spans: [span(R1, 'wang@example.com', 'email')],
      },
      { index: 2, document_name: 'b.docx', chunk_index: 1, included: true, text: R2, spans: [] },
    ],
    messages: [{ role: 'system', content: 'sys' }, { role: 'user', content: Q }],
    edited_parts: 0,
    excluded_references: 0,
    sensitive_count: 2,
    ...overrides,
  }
}

/** A stand-in for the server: it "scans" with the same two patterns. */
function fakePreview(base: ReviewPayload) {
  return vi.fn(async (edits: ReviewEditsBody): Promise<ReviewPayload> => {
    const find = (text: string) => {
      const out = []
      const phone = /1[3-9]\d{9}/.exec(text)
      if (phone) out.push({ start: phone.index, end: phone.index + phone[0].length, kind: 'phone' as const })
      const mail = /[\w.]+@[\w.]+\.\w+/.exec(text)
      if (mail) out.push({ start: mail.index, end: mail.index + mail[0].length, kind: 'email' as const })
      return out
    }
    return {
      ...base,
      question: { text: edits.question, spans: find(edits.question) },
      references: base.references.map((r) => {
        const e = edits.references.find((x) => x.index === r.index)!
        return { ...r, included: e.include, text: e.text, spans: e.include ? find(e.text) : [] }
      }),
    }
  })
}

function setup(over: Partial<ReviewPayload> = {}, props: { busy?: boolean; error?: string | null } = {}) {
  const base = review(over)
  const onPreview = fakePreview(base)
  const onApprove = vi.fn()
  const onCancel = vi.fn()
  render(
    <ReviewCard
      busy={props.busy ?? false}
      error={props.error ?? null}
      onApprove={onApprove}
      onCancel={onCancel}
      onPreview={onPreview}
      review={base}
    />,
  )
  return { onPreview, onApprove, onCancel }
}

const marks = () => screen.queryAllByRole('button', { name: 'chat.review.markAria' })

describe('ReviewCard', () => {
  beforeEach(() => vi.clearAllMocks())

  it('highlights every suspected item as a clickable mark and counts them', () => {
    setup()
    expect(marks().map((m) => m.textContent)).toEqual(['13812345678', 'wang@example.com'])
    expect(screen.getByText(/chat\.review\.found/)).toBeInTheDocument()
  })

  it('says where the content is going, and that a local model stays local', () => {
    setup()
    expect(screen.getByText('chat.review.toVendor')).toBeInTheDocument()
  })

  it('deletes one flagged item and sends the text without it, unchanged elsewhere', async () => {
    const { onApprove } = setup()
    fireEvent.click(marks()[0])
    fireEvent.click(screen.getByRole('button', { name: 'chat.review.delete' }))
    fireEvent.click(screen.getByRole('button', { name: /chat\.review\.send/ }))
    expect(onApprove).toHaveBeenCalledTimes(1)
    const body: ReviewEditsBody = onApprove.mock.calls[0][0]
    expect(body.question).toBe('请联系  的同事')
    expect(body.references[0].text).toBe(R1) // the other flagged item was left alone
  })

  it('replaces a flagged item with whatever the person types', () => {
    const { onApprove } = setup()
    fireEvent.click(marks()[1])
    const input = screen.getByRole('textbox', { name: 'chat.review.replaceWith' })
    fireEvent.change(input, { target: { value: '【财务邮箱】' } })
    fireEvent.click(screen.getByRole('button', { name: 'chat.review.replace' }))
    fireEvent.click(screen.getByRole('button', { name: /chat\.review\.send/ }))
    expect(onApprove.mock.calls[0][0].references[0].text).toBe('财务王芳，邮箱 【财务邮箱】。')
  })

  it('never marks the wrong characters when the text changed but was not re-scanned yet', () => {
    setup()
    fireEvent.click(screen.getAllByRole('button', { name: 'chat.review.edit' })[0])
    // Longer than the old offsets, so they would still be "in range" if reused.
    fireEvent.change(screen.getByRole('textbox', { name: 'chat.review.edit' }), {
      target: { value: 'A'.repeat(40) },
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'chat.review.doneEditing' })[0])
    // Checked at once, before the debounced re-scan can answer.
    expect(marks().map((m) => m.textContent)).toEqual(['wang@example.com'])
  })

  it('does not show a highlight for text that was changed and not yet re-scanned', async () => {
    setup()
    fireEvent.click(marks()[0])
    fireEvent.click(screen.getByRole('button', { name: 'chat.review.delete' }))
    // Right away: the old offsets no longer describe the text, so nothing is marked there.
    expect(marks().map((m) => m.textContent)).toEqual(['wang@example.com'])
  })

  it('re-scans after the person stops editing and follows the new text', async () => {
    const { onPreview } = setup()
    fireEvent.click(screen.getAllByRole('button', { name: 'chat.review.edit' })[0])
    const box = screen.getByRole('textbox', { name: 'chat.review.edit' })
    fireEvent.change(box, { target: { value: '改写后的问题，手机 13700001111' } })
    await waitFor(() => expect(onPreview).toHaveBeenCalled(), { timeout: 2000 })
    const sent: ReviewEditsBody = onPreview.mock.calls.at(-1)![0]
    expect(sent.question).toBe('改写后的问题，手机 13700001111')
    fireEvent.click(screen.getAllByRole('button', { name: 'chat.review.doneEditing' })[0])
    await waitFor(
      () => expect(marks().map((m) => m.textContent)).toContain('13700001111'),
      { timeout: 2000 },
    )
  })

  it('lets any text be edited freely, not only the flagged parts', () => {
    const { onApprove } = setup()
    const editButtons = screen.getAllByRole('button', { name: 'chat.review.edit' })
    fireEvent.click(editButtons[2]) // the second reference passage
    const boxes = screen.getAllByRole('textbox', { name: 'chat.review.edit' })
    fireEvent.change(boxes[0], { target: { value: '完全重写的第二段' } })
    fireEvent.click(screen.getByRole('button', { name: /chat\.review\.send/ }))
    expect(onApprove.mock.calls[0][0].references[1].text).toBe('完全重写的第二段')
  })

  it('"delete all" and "replace all" clear every flagged item at once', () => {
    const { onApprove } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'chat.review.deleteAll' }))
    fireEvent.click(screen.getByRole('button', { name: /chat\.review\.send/ }))
    const body: ReviewEditsBody = onApprove.mock.calls[0][0]
    expect(body.question).toBe('请联系  的同事')
    expect(body.references[0].text).toBe('财务王芳，邮箱 。')
  })

  it('"replace all" uses a placeholder named after the kind', () => {
    const { onApprove } = setup()
    fireEvent.click(screen.getByRole('button', { name: 'chat.review.maskAll' }))
    fireEvent.click(screen.getByRole('button', { name: /chat\.review\.send/ }))
    expect(onApprove.mock.calls[0][0].question).toBe('请联系 [chat.review.kind.phone] 的同事')
  })

  it('drops an unchecked reference from what is sent', () => {
    const { onApprove } = setup()
    const first = screen.getAllByRole('checkbox')[0]
    fireEvent.click(first)
    fireEvent.click(screen.getByRole('button', { name: /chat\.review\.send/ }))
    const refs = onApprove.mock.calls[0][0].references
    expect(refs.map((r: { include: boolean }) => r.include)).toEqual([false, true])
  })

  it('cannot send when every reference is unchecked, and says why', () => {
    setup()
    for (const box of screen.getAllByRole('checkbox')) fireEvent.click(box)
    expect(screen.getByRole('button', { name: /chat\.review\.send/ })).toBeDisabled()
    expect(screen.getByText('chat.review.noneIncluded')).toBeInTheDocument()
  })

  it('cannot send an empty question', () => {
    setup()
    fireEvent.click(screen.getAllByRole('button', { name: 'chat.review.edit' })[0])
    fireEvent.change(screen.getByRole('textbox', { name: 'chat.review.edit' }), {
      target: { value: '   ' },
    })
    expect(screen.getByRole('button', { name: /chat\.review\.send/ })).toBeDisabled()
  })

  it('cancel does not send, and send is blocked while a reply is already starting', () => {
    const { onCancel, onApprove } = setup()
    fireEvent.click(screen.getByRole('button', { name: /chat\.review\.cancel/ }))
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(onApprove).not.toHaveBeenCalled()
  })

  it('shows the exact full content on request, and a refused edit', () => {
    setup({}, { error: 'bad edit' })
    expect(screen.getByText('bad edit')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'chat.review.showRaw' }))
    const pre = document.querySelector('.review-raw-body') as HTMLElement
    expect(within(pre).getByText(/sys/)).toBeInTheDocument()
  })

  it('reports no sensitive items when none are found', () => {
    setup({
      question: { text: '普通问题', spans: [] },
      references: [{ index: 1, document_name: 'a', chunk_index: 0, included: true, text: '普通', spans: [] }],
      sensitive_count: 0,
    })
    expect(screen.getByText(/chat\.review\.none/)).toBeInTheDocument()
    expect(marks()).toEqual([])
  })
})
