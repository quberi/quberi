import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AdminApiError, type OAuthClientList, type OAuthClientRow } from './api'
import { ClientsView } from './ClientsView'

const api = vi.hoisted(() => ({
  listOAuthClients: vi.fn(),
  createOAuthClient: vi.fn(),
  updateOAuthClient: vi.fn(),
  deleteOAuthClient: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const real = await importOriginal<typeof import('./api')>()
  return { ...real, ...api }
})

const row = (over: Partial<OAuthClientRow> = {}): OAuthClientRow => ({
  id: 'r1',
  client_id: 'quberi-mcp',
  kind: 'registered',
  name: 'MCP client',
  redirect_uris: ['loopback', 'https://claude.ai/api/mcp/auth_callback'],
  enabled: true,
  allow_mcp: true,
  note: null,
  created_at: '2026-10-21T00:00:00Z',
  updated_at: '2026-10-21T00:00:00Z',
  ...over,
})

const list = (over: Partial<OAuthClientList> = {}): OAuthClientList => ({
  items: [row()],
  rejected: [],
  advertises_metadata_documents: false,
  ...over,
})

describe('ClientsView', () => {
  beforeEach(() => {
    Object.values(api).forEach((f) => f.mockReset())
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  it('lists each tool with its id and exactly where it may be sent its code', async () => {
    api.listOAuthClients.mockResolvedValue(list())
    render(<ClientsView token="t" />)
    expect(await screen.findByText('MCP client')).toBeTruthy()
    const table = within(screen.getAllByRole('table')[0])
    expect(table.getByText('quberi-mcp')).toBeTruthy()
    expect(table.getByText('loopback')).toBeTruthy()
    expect(table.getByText('https://claude.ai/api/mcp/auth_callback')).toBeTruthy()
  })

  it('says whether metadata documents are advertised', async () => {
    api.listOAuthClients.mockResolvedValue(list({ advertises_metadata_documents: true }))
    render(<ClientsView token="t" />)
    const line = await screen.findByTestId('advertising')
    expect(line.textContent).toContain('advertised')
    expect(line.textContent).not.toContain('not advertised')
  })

  it('switches a tool off and on again', async () => {
    api.listOAuthClients.mockResolvedValue(list())
    api.updateOAuthClient.mockResolvedValue(row({ enabled: false }))
    render(<ClientsView token="t" />)
    fireEvent.click(await screen.findByText('on'))
    await waitFor(() => expect(api.updateOAuthClient).toHaveBeenCalledWith('t', 'r1', { enabled: false }))
  })

  it('removes a tool only after confirming', async () => {
    api.listOAuthClients.mockResolvedValue(list())
    api.deleteOAuthClient.mockResolvedValue(undefined)
    render(<ClientsView token="t" />)
    await screen.findByText('MCP client')
    vi.mocked(window.confirm).mockReturnValueOnce(false)
    fireEvent.click(screen.getByTitle('Remove'))
    expect(api.deleteOAuthClient).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTitle('Remove'))
    await waitFor(() => expect(api.deleteOAuthClient).toHaveBeenCalledWith('t', 'r1'))
  })

  it('adds a tool, one redirect rule per line, and shows what the server refused', async () => {
    api.listOAuthClients.mockResolvedValue(list())
    api.createOAuthClient.mockRejectedValueOnce(new AdminApiError(400, 'redirect must be an https:// address'))
    render(<ClientsView token="t" />)
    fireEvent.click(await screen.findByText('Add tool'))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByText('Name shown to people').closest('label')!.querySelector('input')!, {
      target: { value: 'Example' },
    })
    fireEvent.change(within(dialog).getByText('Client id').closest('label')!.querySelector('input')!, {
      target: { value: 'https://example.com/{id}/client.json' },
    })
    fireEvent.change(within(dialog).getByRole('textbox', { name: /sent its code to/ }), {
      target: { value: 'loopback\n  http://bad.example/cb  \n' },
    })
    fireEvent.click(within(dialog).getByText('Save'))
    expect(await within(dialog).findByText('redirect must be an https:// address')).toBeTruthy()
    expect(api.createOAuthClient).toHaveBeenCalledWith('t', {
      client_id: 'https://example.com/{id}/client.json',
      kind: 'metadata_url',
      name: 'Example',
      redirect_uris: ['loopback', 'http://bad.example/cb'],
      enabled: true,
      allow_mcp: true,
      note: null,
    })
  })

  it('offers to allow a tool that was just refused, prefilled with what it presented', async () => {
    api.listOAuthClients.mockResolvedValue(
      list({
        rejected: [
          {
            client_id: 'https://claude.ai/oauth/hosted.json',
            redirect_uri: 'https://claude.ai/api/mcp/auth_callback',
            reason: 'unknown_client',
            at: '2026-10-21T10:00:00Z',
          },
          {
            client_id: 'quberi-mcp',
            redirect_uri: 'https://elsewhere.example/cb',
            reason: 'redirect_not_allowed',
            at: '2026-10-21T10:01:00Z',
          },
        ],
      }),
    )
    render(<ClientsView token="t" />)
    const buttons = await screen.findAllByText('Allow…')
    // Only an unknown tool can be allowed from here; a wrong callback is not a one-click fix.
    expect(buttons).toHaveLength(1)
    fireEvent.click(buttons[0])
    const dialog = screen.getByRole('dialog')
    expect((within(dialog).getByText('Client id').closest('label')!.querySelector('input') as HTMLInputElement).value).toBe(
      'https://claude.ai/oauth/hosted.json',
    )
    expect((within(dialog).getByRole('textbox', { name: /sent its code to/ }) as HTMLTextAreaElement).value).toBe(
      'https://claude.ai/api/mcp/auth_callback',
    )
  })

  it('shows the server error when the list cannot be loaded', async () => {
    api.listOAuthClients.mockRejectedValue(new AdminApiError(401, 'invalid platform admin token'))
    render(<ClientsView token="t" />)
    expect(await screen.findByText('invalid platform admin token')).toBeTruthy()
  })
})
