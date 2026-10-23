import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AdminApiError, type TrafficSummary } from './api'
import { bucketSeries, TrafficView } from './TrafficView'

const api = vi.hoisted(() => ({
  getTraffic: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const real = await importOriginal<typeof import('./api')>()
  return { ...real, ...api }
})

const summary = (over: Partial<TrafficSummary> = {}): TrafficSummary => ({
  enabled: true,
  reason: null,
  window: { from: '2026-10-22T08:10:00Z', to: '2026-10-23T08:10:00Z' },
  files_read: 2,
  truncated_by_cap: false,
  address_note: 'Addresses are masked. Header-derived addresses can be forged.',
  totals: { requests: 200, distinct_visitors: 31, automated_requests: 150, automated_share: 0.75 },
  by_site: [
    { host: 'app.example.test', requests: 120 },
    { host: '(unknown host)', requests: 50 },
    { host: 'mcp.example.test', requests: 30 },
  ],
  by_kind: [
    { kind: 'direct_or_unknown_host', requests: 50 },
    { kind: 'search_engine', requests: 20 },
    { kind: 'mcp_directory_or_probe', requests: 40 },
    { kind: 'scanner_or_tool', requests: 40 },
    { kind: 'mcp_client', requests: 10 },
    { kind: 'browser_like', requests: 35 },
    { kind: 'other', requests: 5 },
  ],
  by_status_class: [
    { status_class: '2xx', requests: 90 },
    { status_class: '3xx', requests: 10 },
    { status_class: '4xx', requests: 95 },
    { status_class: '5xx', requests: 5 },
    { status_class: 'other', requests: 0 },
  ],
  by_country: [
    { country: 'US', requests: 80 },
    { country: 'unknown', requests: 60 },
  ],
  mcp: {
    top_user_agents: [
      { user_agent: 'mcpbeat/0.1', kind: 'mcp_directory_or_probe', requests: 25 },
      { user_agent: 'codex-mcp-client/0.163.0-alpha.11', kind: 'mcp_client', requests: 8 },
    ],
    top_paths: [
      { method: 'POST', path: '/', status: 401, requests: 27 },
      { method: 'GET', path: '/.well-known/oauth-protected-resource', status: 404, requests: 3 },
    ],
  },
  web_pages: [{ path: '/pricing', browser_like: 12, search_engine: 7, total: 19 }],
  signals: {
    guest_questions: 4,
    sign_in_requests: 2,
    oauth_requests: 1,
    mcp_successful_posts: 6,
  },
  top_visitors: [
    {
      address: '203.0.113.x',
      address_source: 'header',
      kind: 'browser_like',
      country: 'US',
      user_agent: 'Mozilla/5.0 (Macintosh)',
      requests: 14,
    },
    {
      address: '2001:db8:1:…',
      address_source: 'remote_ip',
      kind: 'scanner_or_tool',
      country: 'unknown',
      user_agent: 'curl/8.7.1',
      requests: 9,
    },
  ],
  hourly: [
    { hour: '2026-10-23T06:00:00Z', requests: 20, automated_requests: 15 },
    { hour: '2026-10-23T07:00:00Z', requests: 0, automated_requests: 0 },
    { hour: '2026-10-23T08:00:00Z', requests: 180, automated_requests: 135 },
  ],
  ...over,
})

const disabled = (reason: TrafficSummary['reason']): TrafficSummary =>
  summary({
    enabled: false,
    reason,
    totals: { requests: 0, distinct_visitors: 0, automated_requests: 0, automated_share: 0 },
    by_site: [],
    by_country: [],
    mcp: { top_user_agents: [], top_paths: [] },
    web_pages: [],
    top_visitors: [],
    hourly: [],
  })

const kpi = (label: string) => {
  const labelNode = screen.getByText(label, { selector: '.admin-kpi-label' })
  return labelNode.parentElement?.textContent ?? ''
}

describe('TrafficView', () => {
  beforeEach(() => {
    api.getTraffic.mockReset()
    window.localStorage.clear()
  })

  it('shows the summary cards and every table from the response', async () => {
    api.getTraffic.mockResolvedValue(summary())
    render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText('Distinct visitors')).toBeTruthy()

    expect(kpi('Requests')).toContain('200')
    expect(kpi('Distinct visitors')).toContain('31')
    expect(kpi('Automated share')).toContain('75.0%')
    expect(kpi('Automated share')).toContain('150 automated requests')
    expect(kpi('Guest questions')).toContain('4')
    expect(kpi('Sign-in requests')).toContain('2')
    expect(kpi('OAuth requests')).toContain('1')
    expect(kpi('MCP calls accepted')).toContain('6')

    const kinds = within(screen.getByRole('table', { name: 'Requests by kind of visitor' }))
    expect(kinds.getByText('Scanners and tools')).toBeTruthy()
    const scannerRow = kinds.getByText('Scanners and tools').closest('tr') as HTMLElement
    expect(scannerRow.textContent).toContain('40')
    expect(scannerRow.textContent).toContain('20.0%') // 40 of 200
    expect(kinds.getAllByRole('row')).toHaveLength(8) // header + 7 kinds

    expect(within(screen.getByRole('table', { name: 'Requests by site' })).getByText('(unknown host)')).toBeTruthy()
    expect(within(screen.getByRole('table', { name: 'Requests by country' })).getByText('US')).toBeTruthy()
    expect(within(screen.getByRole('table', { name: 'Requests by response class' })).getByText('4xx')).toBeTruthy()
    const agents = within(screen.getByRole('table', { name: 'MCP user agents' }))
    expect(agents.getByText('mcpbeat/0.1')).toBeTruthy()
    expect(agents.getByText('MCP clients')).toBeTruthy()
    const paths = within(screen.getByRole('table', { name: 'MCP paths' }))
    expect(paths.getByText('/.well-known/oauth-protected-resource')).toBeTruthy()
    expect(paths.getByText('401')).toBeTruthy()
    const visitors = within(screen.getByRole('table', { name: 'Top visitors' }))
    expect(visitors.getByText('203.0.113.x')).toBeTruthy()
    expect(visitors.getByText('header')).toBeTruthy()
    expect(visitors.getByText('remote_ip')).toBeTruthy()
    const pages = within(screen.getByRole('table', { name: 'Web pages' }))
    expect(pages.getByText('/pricing')).toBeTruthy()
    expect(pages.getByText('19')).toBeTruthy()
  })

  it('has column headers on every table and an accessible chart', async () => {
    api.getTraffic.mockResolvedValue(summary())
    const { container } = render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('Distinct visitors')
    const tables = screen.getAllByRole('table')
    expect(tables).toHaveLength(8)
    for (const table of tables) {
      expect(table.getAttribute('aria-label')).toBeTruthy()
      expect(within(table).getAllByRole('columnheader').length).toBeGreaterThan(1)
    }
    const chart = screen.getByRole('img')
    expect(chart.getAttribute('aria-label')).toContain('200 in total')
    expect(container.querySelectorAll('.admin-trend-bar')).toHaveLength(3)
  })

  it('says plainly that the kinds are inferred and the addresses masked', async () => {
    api.getTraffic.mockResolvedValue(summary())
    render(<TrafficView token="t" onSignOut={() => {}} />)
    const note = await screen.findByText(/inferred from the User-Agent/)
    expect(note.textContent).toContain('can still be a crawler')
    expect(note.textContent).toContain('Addresses are masked.')
    expect(note.textContent).toContain('Header-derived addresses can be forged.')
  })

  it('shows empty tables as "No requests in this window."', async () => {
    api.getTraffic.mockResolvedValue(
      summary({
        web_pages: [],
        top_visitors: [],
        mcp: { top_user_agents: [], top_paths: [] },
        by_country: [],
      }),
    )
    render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('Distinct visitors')
    expect(screen.getAllByText('No requests in this window.')).toHaveLength(5)
  })

  it.each([
    ['not_configured', /No access log is configured/],
    ['file_missing', /no file there/],
    ['unreadable', /not allowed to read it/],
  ] as const)('explains how to turn it on when it is off (%s)', async (reason, text) => {
    api.getTraffic.mockResolvedValue(disabled(reason))
    render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText('Traffic is not set up')).toBeTruthy()
    expect(screen.getByText(text)).toBeTruthy()
    const panel = screen.getByRole('status')
    expect(panel.textContent).toContain('caddy-data:/caddy-data:ro')
    expect(panel.textContent).toContain('QUBERI_ACCESS_LOG_PATH')
    expect(panel.textContent).toContain('QUBERI_TRAFFIC_SITE_HOSTS')
    expect(panel.textContent).toContain('Recreate the API container')
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('reloads for the chosen window', async () => {
    api.getTraffic.mockResolvedValue(summary())
    render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('Distinct visitors')
    expect(api.getTraffic).toHaveBeenLastCalledWith('t', { hours: 24, excludeIps: '' })
    expect(screen.getByRole('button', { name: '24h' }).getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(screen.getByRole('button', { name: '7d' }))
    await waitFor(() =>
      expect(api.getTraffic).toHaveBeenLastCalledWith('t', { hours: 168, excludeIps: '' }),
    )
    expect(screen.getByRole('button', { name: '7d' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: '24h' }).getAttribute('aria-pressed')).toBe('false')

    fireEvent.click(screen.getByRole('button', { name: '1h' }))
    await waitFor(() =>
      expect(api.getTraffic).toHaveBeenLastCalledWith('t', { hours: 1, excludeIps: '' }),
    )
    fireEvent.click(screen.getByRole('button', { name: '14d' }))
    await waitFor(() =>
      expect(api.getTraffic).toHaveBeenLastCalledWith('t', { hours: 336, excludeIps: '' }),
    )
  })

  it('refreshes on demand', async () => {
    api.getTraffic.mockResolvedValue(summary())
    render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('Distinct visitors')
    const before = api.getTraffic.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() => expect(api.getTraffic.mock.calls.length).toBe(before + 1))
  })

  it('remembers the excluded addresses and sends them', async () => {
    api.getTraffic.mockResolvedValue(summary())
    const first = render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('Distinct visitors')

    const input = screen.getByLabelText('Exclude my addresses') as HTMLInputElement
    fireEvent.change(input, { target: { value: ' 203.0.113.9, 2001:db8::1 ' } })
    // Typing alone neither saves nor refetches
    expect(window.localStorage.getItem('quberi.admin.traffic.exclude')).toBeNull()
    expect(api.getTraffic).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() =>
      expect(api.getTraffic).toHaveBeenLastCalledWith('t', {
        hours: 24,
        excludeIps: '203.0.113.9, 2001:db8::1',
      }),
    )
    expect(window.localStorage.getItem('quberi.admin.traffic.exclude')).toBe(
      '203.0.113.9, 2001:db8::1',
    )

    // A later visit starts with them filled in and applied
    first.unmount()
    api.getTraffic.mockClear()
    render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('Distinct visitors')
    expect((screen.getByLabelText('Exclude my addresses') as HTMLInputElement).value).toBe(
      '203.0.113.9, 2001:db8::1',
    )
    expect(api.getTraffic).toHaveBeenLastCalledWith('t', {
      hours: 24,
      excludeIps: '203.0.113.9, 2001:db8::1',
    })
  })

  it('applies the excluded addresses on Enter and forgets them when cleared', async () => {
    window.localStorage.setItem('quberi.admin.traffic.exclude', '192.0.2.1')
    api.getTraffic.mockResolvedValue(summary())
    render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('Distinct visitors')
    const input = screen.getByLabelText('Exclude my addresses')
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)
    await waitFor(() =>
      expect(api.getTraffic).toHaveBeenLastCalledWith('t', { hours: 24, excludeIps: '' }),
    )
    expect(window.localStorage.getItem('quberi.admin.traffic.exclude')).toBeNull()
  })

  it('shows a User-Agent and a path full of HTML characters as plain text', async () => {
    const evilUa = '<img src=x onerror=alert(1)>'
    const evilPath = '/<script>alert(2)</script>'
    api.getTraffic.mockResolvedValue(
      summary({
        top_visitors: [
          {
            address: '203.0.113.x',
            address_source: 'header',
            kind: 'scanner_or_tool',
            country: 'US',
            user_agent: evilUa,
            requests: 3,
          },
        ],
        mcp: {
          top_user_agents: [{ user_agent: evilUa, kind: 'other', requests: 3 }],
          top_paths: [{ method: 'GET', path: evilPath, status: 404, requests: 3 }],
        },
        web_pages: [{ path: evilPath, browser_like: 1, search_engine: 0, total: 1 }],
        by_site: [{ host: '<b>host</b>', requests: 1 }],
      }),
    )
    const { container } = render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('Distinct visitors')
    expect(screen.getAllByText(evilUa).length).toBeGreaterThan(0)
    expect(screen.getAllByText(evilPath).length).toBeGreaterThan(0)
    expect(screen.getByText('<b>host</b>')).toBeTruthy()
    expect(container.querySelector('img, script, b')).toBeNull()
  })

  it('signs out when the session has expired', async () => {
    api.getTraffic.mockRejectedValue(new AdminApiError(401, 'invalid platform admin token'))
    const onSignOut = vi.fn()
    render(<TrafficView token="t" onSignOut={onSignOut} />)
    await waitFor(() => expect(onSignOut).toHaveBeenCalledTimes(1))
  })

  it('shows the server message for any other failure', async () => {
    api.getTraffic.mockRejectedValue(new AdminApiError(500, 'something broke'))
    const onSignOut = vi.fn()
    render(<TrafficView token="t" onSignOut={onSignOut} />)
    expect(await screen.findByText(/something broke/)).toBeTruthy()
    expect(onSignOut).not.toHaveBeenCalled()
  })

  it('warns when the log was larger than the read limit', async () => {
    api.getTraffic.mockResolvedValue(summary({ truncated_by_cap: true }))
    render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText(/only its newest part is included/)).toBeTruthy()
  })

  it('does not warn when nothing was cut', async () => {
    api.getTraffic.mockResolvedValue(summary())
    render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('Distinct visitors')
    expect(screen.queryByText(/only its newest part is included/)).toBeNull()
  })

  it('ignores a slow earlier answer that arrives after a newer one', async () => {
    let releaseSlow: (v: TrafficSummary) => void = () => {}
    api.getTraffic.mockImplementationOnce(
      () => new Promise<TrafficSummary>((resolve) => (releaseSlow = resolve)),
    )
    api.getTraffic.mockResolvedValueOnce(
      summary({ totals: { requests: 777, distinct_visitors: 1, automated_requests: 0, automated_share: 0 } }),
    )
    render(<TrafficView token="t" onSignOut={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: '7d' }))
    await screen.findByText('777')
    releaseSlow(
      summary({ totals: { requests: 111, distinct_visitors: 1, automated_requests: 0, automated_share: 0 } }),
    )
    await new Promise((r) => setTimeout(r, 10))
    expect(screen.getByText('777')).toBeTruthy()
    expect(screen.queryByText('111')).toBeNull()
  })
})

describe('bucketSeries', () => {
  const hourly = [
    { hour: '2026-10-21T22:00:00Z', requests: 1, automated_requests: 1 },
    { hour: '2026-10-21T23:00:00Z', requests: 2, automated_requests: 0 },
    { hour: '2026-10-22T00:00:00Z', requests: 4, automated_requests: 3 },
    { hour: '2026-10-22T01:00:00Z', requests: 8, automated_requests: 4 },
  ]

  it('keeps one bar per hour for windows up to two days', () => {
    const bars = bucketSeries(hourly, 24)
    expect(bars).toHaveLength(4)
    expect(bars[0]).toEqual({ key: '2026-10-21T22:00:00Z', label: '22', requests: 1, automated: 1 })
    expect(bucketSeries(hourly, 48)).toHaveLength(4)
  })

  it('sums the hours of each UTC day for longer windows', () => {
    const bars = bucketSeries(hourly, 168)
    expect(bars).toEqual([
      { key: '2026-10-21', label: '10-21', requests: 3, automated: 1 },
      { key: '2026-10-22', label: '10-22', requests: 12, automated: 7 },
    ])
  })

  it('is empty without data', () => {
    expect(bucketSeries([], 24)).toEqual([])
    expect(bucketSeries([], 336)).toEqual([])
  })
})
