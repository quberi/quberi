import { Check, Copy } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

/** Address of this deployment's MCP server. A self-hosted copy can set VITE_MCP_URL. */
export const MCP_URL = (import.meta.env.VITE_MCP_URL ?? 'https://mcp.quberi.com').replace(/\/+$/, '')

/** Client ids written into the person's own tool config. Each is a row in the platform's tool list. */
export const CODEX_CLIENT_ID = 'quberi-mcp'
export const CURSOR_CLIENT_ID = 'quberi-cursor'

export const CODEX_COMMANDS = [
  `codex mcp add quberi --url ${MCP_URL} --oauth-client-id ${CODEX_CLIENT_ID}`,
  'codex mcp login quberi',
].join('\n')

export const CLAUDE_CODE_COMMAND = `claude mcp add --transport http quberi ${MCP_URL}`

export const CURSOR_CONFIG = JSON.stringify(
  { mcpServers: { quberi: { url: MCP_URL, auth: { CLIENT_ID: CURSOR_CLIENT_ID } } } },
  null,
  2,
)

/** A block of text to copy, with a button. Falls back quietly when the clipboard is unavailable. */
function CopyBlock({ text, multiline = false }: { text: string; multiline?: boolean }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearTimeout(timer.current), [])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => setCopied(false), 1800)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className="connect-help-copy">
      {multiline ? <pre>{text}</pre> : <code>{text}</code>}
      <button className="ghost-button" onClick={() => void copy()} type="button">
        {copied ? <Check size={13} /> : <Copy size={13} />}
        <span>{copied ? t('connectTools.copied') : t('connectTools.copy')}</span>
      </button>
    </div>
  )
}

/**
 * How a person connects an outside AI tool to their knowledge. Shown whether or not anything
 * is connected yet, because the first connection is exactly when it is needed.
 */
export function ConnectToolsHelp() {
  const { t } = useTranslation()
  return (
    <>
      <details className="connect-help">
        <summary>
          <strong>{t('connectTools.title')}</strong>
          <span>{t('connectTools.hint')}</span>
        </summary>

        <div className="connect-help-body">
          <p className="connect-help-label">{t('connectTools.urlLabel')}</p>
          <CopyBlock text={MCP_URL} />

          <section>
            <h3>{t('connectTools.claude.title')}</h3>
            <p>{t('connectTools.claude.steps')}</p>
          </section>

          <section>
            <h3>{t('connectTools.chatgpt.title')}</h3>
            <p>{t('connectTools.chatgpt.steps')}</p>
          </section>

          <section>
            <h3>{t('connectTools.codex.title')}</h3>
            <p>{t('connectTools.codex.steps')}</p>
            <CopyBlock text={CODEX_COMMANDS} multiline />
          </section>

          <section>
            <h3>{t('connectTools.claudeCode.title')}</h3>
            <p>{t('connectTools.claudeCode.steps')}</p>
            <CopyBlock text={CLAUDE_CODE_COMMAND} />
          </section>

          <section>
            <h3>{t('connectTools.cursor.title')}</h3>
            <p>{t('connectTools.cursor.steps')}</p>
            <CopyBlock text={CURSOR_CONFIG} multiline />
          </section>

          <p className="connect-help-note">{t('connectTools.note')}</p>
        </div>
      </details>
      <div className="account-divider" />
    </>
  )
}
