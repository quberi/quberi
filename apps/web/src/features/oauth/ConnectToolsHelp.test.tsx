import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  CLAUDE_CODE_COMMAND,
  CODEX_COMMANDS,
  CURSOR_CONFIG,
  ConnectToolsHelp,
  MCP_URL,
} from './ConnectToolsHelp'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { resolvedLanguage: 'en-US', changeLanguage: () => undefined },
  }),
}))

afterEach(() => {
  vi.restoreAllMocks()
})

describe('snippets', () => {
  it('point at the server address without a path', () => {
    expect(MCP_URL).toBe('https://mcp.quberi.com')
  })

  it('give Codex the shared client id, and log in', () => {
    expect(CODEX_COMMANDS).toBe(
      'codex mcp add quberi --url https://mcp.quberi.com --oauth-client-id quberi-mcp\ncodex mcp login quberi',
    )
  })

  it('add Claude Code over http with no client id', () => {
    expect(CLAUDE_CODE_COMMAND).toBe('claude mcp add --transport http quberi https://mcp.quberi.com')
  })

  it('give Cursor its own client id, as valid JSON', () => {
    expect(JSON.parse(CURSOR_CONFIG)).toEqual({
      mcpServers: { quberi: { url: 'https://mcp.quberi.com', auth: { CLIENT_ID: 'quberi-cursor' } } },
    })
  })
})

describe('ConnectToolsHelp', () => {
  it('is collapsed by default and offers one tab per tool', () => {
    const { container } = render(<ConnectToolsHelp />)
    const details = container.querySelector('details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(screen.getAllByRole('tab')).toHaveLength(5)
    expect(container.textContent).toContain(MCP_URL)
  })

  it('shows one tool at a time, starting with Claude', () => {
    render(<ConnectToolsHelp />)
    expect(screen.getByRole('tab', { name: 'connectTools.claude.tab' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByText('connectTools.claude.title')).toBeTruthy()
    expect(screen.queryByText('connectTools.cursor.title')).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: 'connectTools.cursor.tab' }))
    expect(screen.getByText('connectTools.cursor.title')).toBeTruthy()
    expect(screen.queryByText('connectTools.claude.title')).toBeNull()
  })

  it('copies the address, and the snippet of the chosen tool', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<ConnectToolsHelp />)
    fireEvent.click(screen.getByRole('button', { name: /connectTools.copy/ }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(MCP_URL))
    fireEvent.click(screen.getByRole('tab', { name: 'connectTools.cursor.tab' }))
    const buttons = screen.getAllByRole('button', { name: /connectTools.cop/ })
    expect(buttons).toHaveLength(2)
    fireEvent.click(buttons[1]!)
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(CURSOR_CONFIG))
    expect(await screen.findAllByText('connectTools.copied')).toHaveLength(2)
  })

  it('stays quiet when the clipboard refuses', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<ConnectToolsHelp />)
    fireEvent.click(screen.getByRole('button', { name: /connectTools.copy/ }))
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    expect(screen.queryByText('connectTools.copied')).toBeNull()
  })
})
