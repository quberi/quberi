import { Check, ChevronDown, Copy, Plug } from 'lucide-react'
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

const TOOLS = ['claude', 'chatgpt', 'codex', 'claudeCode', 'cursor'] as const
type ToolKey = (typeof TOOLS)[number]

/** The text to copy for each tool; Claude and ChatGPT only need the address. */
const SNIPPETS: Record<ToolKey, { text: string; multiline: boolean } | null> = {
  claude: null,
  chatgpt: null,
  codex: { text: CODEX_COMMANDS, multiline: true },
  claudeCode: { text: CLAUDE_CODE_COMMAND, multiline: false },
  cursor: { text: CURSOR_CONFIG, multiline: true },
}

/**
 * How a person connects an outside AI tool to their knowledge. Shown whether or not anything
 * is connected yet, because the first connection is exactly when it is needed. One tool at a
 * time, so the panel stays short.
 */
export function ConnectToolsHelp() {
  const { t } = useTranslation()
  const [active, setActive] = useState<ToolKey>('claude')
  const snippet = SNIPPETS[active]
  return (
    <>
      <div className="connect-help-wrap">
        <details className="connect-help">
          <summary>
            <span className="connect-help-icon" aria-hidden="true">
              <Plug size={18} />
            </span>
            <span className="connect-help-heading">
              <strong>{t('connectTools.title')}</strong>
              <small>{t('connectTools.hint')}</small>
            </span>
            <ChevronDown className="connect-help-chevron" size={18} aria-hidden="true" />
          </summary>

          <div className="connect-help-body">
            <div className="connect-help-field">
              <span className="connect-help-label">{t('connectTools.urlLabel')}</span>
              <CopyBlock text={MCP_URL} />
            </div>

            <div className="connect-help-tabs" role="tablist" aria-label={t('connectTools.title')}>
              {TOOLS.map((k) => (
                <button
                  key={k}
                  aria-selected={active === k}
                  className={active === k ? 'connect-help-tab active' : 'connect-help-tab'}
                  id={`connect-tab-${k}`}
                  onClick={() => setActive(k)}
                  role="tab"
                  type="button"
                >
                  {t(`connectTools.${k}.tab`)}
                </button>
              ))}
            </div>

            <div
              aria-labelledby={`connect-tab-${active}`}
              className="connect-help-panel"
              role="tabpanel"
            >
              <h3>{t(`connectTools.${active}.title`)}</h3>
              <p>{t(`connectTools.${active}.steps`)}</p>
              {snippet && <CopyBlock text={snippet.text} multiline={snippet.multiline} />}
            </div>

            <p className="connect-help-note">{t('connectTools.note')}</p>
          </div>
        </details>
      </div>
      <div className="account-divider" />
    </>
  )
}
