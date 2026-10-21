import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'
import { OverviewDashboard } from './OverviewDashboard'
import type { Overview } from './types'

const api = vi.hoisted(() => ({ getOverview: vi.fn(), viewerTzOffset: () => 0 }))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    // Echo the key with its numbers so a test can see which sentence was chosen.
    t: (k: string, o?: Record<string, unknown>) =>
      o && (o.count !== undefined || o.days !== undefined)
        ? `${k}#${o.count ?? o.days}${o.names ? `|${o.names}` : ''}`
        : k,
    i18n: { language: 'en-US' },
  }),
}))
const auth = vi.hoisted(() => ({ credentials: { sessionToken: 't' } }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('./api', () => api)
vi.mock('../audit/LivePulseCard', () => ({ LivePulseCard: () => <div>live-pulse</div> }))

const base = (over: Partial<Overview> = {}): Overview => ({
  members: { total: 12, active: 10, invited: 1, suspended: 1 },
  spaces: { total: 6, enterprise: 2, team: 3, personal: 1, archived: 0, frozen: 0 },
  documents: { total: 40, indexed: 38, failed: 0, waiting: 2 },
  external: { connected_tools: 3, calls_7d: 25 },
  attention: [],
  trend: Array.from({ length: 14 }, (_, i) => ({
    day: `2026-10-${String(i + 1).padStart(2, '0')}`,
    documents: i,
    questions: i * 2,
  })),
  top_cited: [],
  cited_days: 30,
  ...over,
})

describe('OverviewDashboard', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows the headline numbers and only the parts of each that are not zero', async () => {
    api.getOverview.mockResolvedValue(base())
    render(<OverviewDashboard onNavigate={() => undefined} />)
    expect(await screen.findByText('12')).toBeTruthy()
    expect(screen.getByText('overview.stat.membersInvited#1')).toBeTruthy()
    expect(screen.getByText('overview.stat.membersSuspended#1')).toBeTruthy()
    expect(screen.getByText('overview.stat.docsWaiting#2')).toBeTruthy()
    // Nothing failed and nothing is archived, so neither is mentioned.
    expect(screen.queryByText(/docsFailed/)).toBeNull()
    expect(screen.queryByText(/spacesArchived/)).toBeNull()
    expect(screen.getByText('live-pulse')).toBeTruthy()
  })

  it('has no attention block when there is nothing to attend to', async () => {
    api.getOverview.mockResolvedValue(base())
    render(<OverviewDashboard onNavigate={() => undefined} />)
    await screen.findByText('12')
    expect(screen.queryByText('overview.attention.title')).toBeNull()
  })

  it('lists what needs attention and sends the admin to the right page', async () => {
    api.getOverview.mockResolvedValue(
      base({
        attention: [
          { code: 'pending_reviews', count: 3, names: [] },
          { code: 'suspended_owning_spaces', count: 1, names: ['Old Ben'] },
        ],
      }),
    )
    const go = vi.fn()
    render(<OverviewDashboard onNavigate={go} />)
    expect(await screen.findByText('overview.attention.pending_reviews#3')).toBeTruthy()
    expect(screen.getByText(/overview\.attention\.suspended_owning_spaces#1\|.*Old Ben/)).toBeTruthy()
    const buttons = screen.getAllByText(/overview\.attention\.go_/)
    fireEvent.click(buttons[0].closest('button') as HTMLButtonElement)
    fireEvent.click(buttons[1].closest('button') as HTMLButtonElement)
    expect(go.mock.calls.map((c) => c[0])).toEqual(['spaces', 'members'])
  })

  it('jumps to the page behind a stat card', async () => {
    api.getOverview.mockResolvedValue(base())
    const go = vi.fn()
    render(<OverviewDashboard onNavigate={go} />)
    await screen.findByText('12')
    fireEvent.click(screen.getByText('overview.stat.members').closest('button') as HTMLButtonElement)
    fireEvent.click(screen.getByText('overview.stat.external').closest('button') as HTMLButtonElement)
    expect(go.mock.calls.map((c) => c[0])).toEqual(['members', 'integrations'])
  })

  it('charts fourteen days with their totals', async () => {
    api.getOverview.mockResolvedValue(base())
    render(<OverviewDashboard onNavigate={() => undefined} />)
    await screen.findByText('12')
    expect(document.querySelectorAll('.trend-bar')).toHaveLength(28)
    // 0+1+…+13 = 91 documents; double that for questions.
    expect(screen.getByText('91')).toBeTruthy()
    expect(screen.getByText('182')).toBeTruthy()
  })

  it('ranks the most cited documents and says so when there are none', async () => {
    api.getOverview.mockResolvedValue(
      base({
        top_cited: [
          { document_id: 'a', name: 'policy.pdf', space_id: 's', space_name: 'Policies', citations: 9, last_cited_at: '' },
          { document_id: 'b', name: 'faq.md', space_id: 's', space_name: 'Policies', citations: 3, last_cited_at: '' },
        ],
      }),
    )
    const { unmount } = render(<OverviewDashboard onNavigate={() => undefined} />)
    expect(await screen.findByText('policy.pdf')).toBeTruthy()
    const names = Array.from(document.querySelectorAll('.overview-cited-name strong')).map((e) => e.textContent)
    expect(names).toEqual(['policy.pdf', 'faq.md'])
    unmount()

    api.getOverview.mockResolvedValue(base())
    render(<OverviewDashboard onNavigate={() => undefined} />)
    expect(await screen.findByText('overview.cited.empty')).toBeTruthy()
  })

  it('shows the server error', async () => {
    api.getOverview.mockRejectedValue(new ApiError(403, 'admins only'))
    render(<OverviewDashboard onNavigate={() => undefined} />)
    await waitFor(() => expect(screen.getByText('admins only')).toBeTruthy())
  })
})
