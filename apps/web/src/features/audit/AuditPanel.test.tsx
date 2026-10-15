import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AuditPanel } from './AuditPanel'
import type { RecentEvent } from './types'

const search = vi.fn()

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: { defaultValue?: string }) => opts?.defaultValue ?? key,
  }),
}))
vi.mock('../../i18n/formatDate', () => ({ useFormatDate: () => (iso: string) => iso }))
// A stable object, like the real auth context's.
const credentials = { token: 'admin' }
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ credentials }) }))
vi.mock('./api', () => ({
  searchAuditEvents: (...args: unknown[]) => search(...args),
  listAuditActions: () => Promise.resolve({ items: ['message.sent', 'space.created'] }),
  exportAuditCsv: () => Promise.resolve(new Blob(['x'])),
}))
vi.mock('../members/api', () => ({
  listMembers: () =>
    Promise.resolve({ items: [{ id: 'u1', display_name: 'Alice', email: 'a@x.com' }] }),
}))
vi.mock('../spaces/api', () => ({
  listOrganizationSpaces: () => Promise.resolve({ items: [{ id: 's1', name: 'HR' }] }),
}))

function event(id: string, over: Partial<RecentEvent> = {}): RecentEvent {
  return {
    id,
    created_at: `2026-10-01T12:00:0${id}Z`,
    actor_type: 'user',
    actor_id: 'u1',
    actor_display_name: 'Alice',
    action: 'space.created',
    resource_type: 'space',
    resource_id: null,
    resource_display_name: 'HR',
    space_id: 's1',
    space_display_name: 'HR',
    payload: {},
    ...over,
  }
}

describe('AuditPanel', () => {
  beforeEach(() => {
    search.mockReset()
    search.mockResolvedValue({ items: [event('1')], next_cursor: null })
  })

  it('loads the last 7 days by default and lists the events', async () => {
    render(<AuditPanel />)
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1))
    const [, filters] = search.mock.calls[0]
    const ageMs = Date.now() - new Date(filters.since).getTime()
    expect(Math.abs(ageMs - 7 * 86_400_000)).toBeLessThan(5_000)
    expect(await screen.findByText('2026-10-01T12:00:01Z')).toBeInTheDocument()
  })

  it('follows the cursor on "load more" and appends without repeating', async () => {
    search
      .mockResolvedValueOnce({ items: [event('1')], next_cursor: 'c1' })
      .mockResolvedValueOnce({ items: [event('2')], next_cursor: null })
    render(<AuditPanel />)
    await screen.findByText('2026-10-01T12:00:01Z')
    fireEvent.click(screen.getByText('audit.page.loadMore'))
    await screen.findByText('2026-10-01T12:00:02Z')
    expect(screen.getByText('2026-10-01T12:00:01Z')).toBeInTheDocument()
    expect(search.mock.calls[1][2]).toMatchObject({ cursor: 'c1' })
    // No more pages: the button is gone.
    expect(screen.queryByText('audit.page.loadMore')).toBeNull()
  })

  it('starts over from the first page when a filter changes', async () => {
    search.mockResolvedValueOnce({ items: [event('1')], next_cursor: 'c1' })
    render(<AuditPanel />)
    await screen.findByText('2026-10-01T12:00:01Z')
    await screen.findByRole('option', { name: 'message.sent' })
    // Dropdown order: time, event, person, space.
    const [, actionSelect] = screen.getAllByRole('combobox')
    fireEvent.change(actionSelect, { target: { value: 'message.sent' } })
    await waitFor(() => expect(search).toHaveBeenCalledTimes(2))
    const [, filters, page] = search.mock.calls[1]
    expect(filters.action).toBe('message.sent')
    expect(page.cursor ?? null).toBeNull()
  })

  it('shows which documents the assistant could see, naming deleted ones', async () => {
    search.mockResolvedValue({
      items: [
        event('1', {
          action: 'message.sent',
          payload: { model_id: 'm', retrieved_document_ids: ['d1', 'd2'] },
          retrieved_documents: [
            { id: 'd1', name: 'handbook.docx' },
            { id: 'd2', name: null },
          ],
        }),
      ],
      next_cursor: null,
    })
    render(<AuditPanel />)
    fireEvent.click(await screen.findByText('2026-10-01T12:00:01Z'))
    expect(await screen.findByText('handbook.docx')).toBeInTheDocument()
    expect(screen.getByText('audit.page.deletedDocument')).toBeInTheDocument()
    // The raw ids are shown as names, not repeated in the JSON block.
    expect(screen.queryByText(/retrieved_document_ids/)).toBeNull()
  })

  it('ignores a slow answer for an old filter', async () => {
    let releaseOld: (v: unknown) => void = () => undefined
    search.mockReset()
    search.mockImplementationOnce(() => new Promise((r) => { releaseOld = r }))
    search.mockResolvedValueOnce({ items: [event('2')], next_cursor: null })
    render(<AuditPanel />)
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1))
    const [timeSelect] = screen.getAllByRole('combobox')
    fireEvent.change(timeSelect, { target: { value: '1h' } })
    await screen.findByText('2026-10-01T12:00:02Z')
    releaseOld({ items: [event('1')], next_cursor: null })
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByText('2026-10-01T12:00:01Z')).toBeNull()
    expect(screen.getByText('2026-10-01T12:00:02Z')).toBeInTheDocument()
  })
})
