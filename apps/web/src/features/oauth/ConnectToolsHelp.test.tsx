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
  it('is collapsed by default and lists every tool when opened', () => {
    const { container } = render(<ConnectToolsHelp />)
    const details = container.querySelector('details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    for (const k of ['claude', 'chatgpt', 'codex', 'claudeCode', 'cursor']) {
      expect(screen.getByText(`connectTools.${k}.title`)).toBeTruthy()
    }
    expect(container.textContent).toContain(MCP_URL)
  })

  it('copies the text beside the button that was clicked', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<ConnectToolsHelp />)
    const buttons = screen.getAllByRole('button', { name: /connectTools.copy/ })
    expect(buttons).toHaveLength(4)
    fireEvent.click(buttons[0]!)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(MCP_URL))
    fireEvent.click(buttons[3]!)
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(CURSOR_CONFIG))
    expect(await screen.findAllByText('connectTools.copied')).toHaveLength(2)
  })

  it('stays quiet when the clipboard refuses', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<ConnectToolsHelp />)
    fireEvent.click(screen.getAllByRole('button', { name: /connectTools.copy/ })[0]!)
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    expect(screen.queryByText('connectTools.copied')).toBeNull()
  })
})
