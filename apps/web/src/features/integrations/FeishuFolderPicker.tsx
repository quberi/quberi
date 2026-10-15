import {
  ChevronDown,
  ChevronRight,
  Folder,
  FileText,
  Loader2,
  RefreshCw,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'

import { feishuBrowseFolders } from './api'
import type { FeishuFolderNode } from './api'

/**
 * Lazy folder-tree picker for Feishu drive. Each folder expands on
 * click, fetching its children on demand (``browse_folder_as_user``).
 * Selecting a folder bubbles its token + name up to the parent form.
 *
 * If the user hasn't authorised Feishu yet, ``onNeedsAuth`` fires so the
 * parent can show the connect button.
 */
export function FeishuFolderPicker({
  selectedToken,
  onSelect,
  onNeedsAuth,
}: {
  selectedToken: string | null
  onSelect: (token: string, name: string) => void
  onNeedsAuth: () => void
}) {
  const { t } = useTranslation()
  return (
    <div className="feishu-folder-picker">
      <FolderLevel
        depth={0}
        folderToken={null}
        onNeedsAuth={onNeedsAuth}
        onSelect={onSelect}
        selectedToken={selectedToken}
      />
    </div>
  )
}

type LevelState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; nodes: FeishuFolderNode[] }

function FolderLevel({
  folderToken,
  depth,
  selectedToken,
  onSelect,
  onNeedsAuth,
}: {
  folderToken: string | null
  depth: number
  selectedToken: string | null
  onSelect: (token: string, name: string) => void
  onNeedsAuth: () => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [state, setState] = useState<LevelState>({ kind: 'idle' })
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // Keep the latest callback in a ref so a parent that passes a fresh function
  // on every render (for example while the user types in the form) does not
  // change ``load`` and reload the whole folder tree.
  const onNeedsAuthRef = useRef(onNeedsAuth)
  onNeedsAuthRef.current = onNeedsAuth
  const load = useCallback(async () => {
    if (!credentials) return
    setState({ kind: 'loading' })
    try {
      const res = await feishuBrowseFolders(credentials, folderToken ?? undefined)
      if (res.needs_auth) {
        onNeedsAuthRef.current()
        setState({ kind: 'ready', nodes: [] })
        return
      }
      setState({ kind: 'ready', nodes: res.items })
    } catch (err) {
      setState({
        kind: 'error',
        message: err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      })
    }
  }, [credentials, folderToken])

  useEffect(() => {
    void load()
  }, [load])

  if (state.kind === 'loading' || state.kind === 'idle') {
    return (
      <div className="feishu-folder-loading" style={{ paddingLeft: depth * 16 }}>
        <Loader2 size={13} className="spin" /> {t('integrations.feishu.picker.loading')}
      </div>
    )
  }
  if (state.kind === 'error') {
    return (
      <div className="feishu-folder-error" style={{ paddingLeft: depth * 16 }}>
        {state.message}
        <button className="link-button" onClick={() => void load()} type="button">
          <RefreshCw size={11} /> {t('integrations.feishu.picker.retry')}
        </button>
      </div>
    )
  }
  if (state.nodes.length === 0) {
    return (
      <div className="feishu-folder-empty" style={{ paddingLeft: depth * 16 + 8 }}>
        {t('integrations.feishu.picker.empty')}
      </div>
    )
  }

  const toggle = (token: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(token)) next.delete(token)
      else next.add(token)
      return next
    })
  }

  return (
    <ul className="feishu-folder-level">
      {state.nodes.map((node) => {
        const isFolder = node.type === 'folder'
        const isOpen = expanded.has(node.token)
        const isSelected = selectedToken === node.token
        return (
          <li key={node.token}>
            <div
              className={`feishu-folder-node ${isSelected ? 'selected' : ''}`}
              style={{ paddingLeft: depth * 16 }}
            >
              {isFolder ? (
                <button
                  className="feishu-folder-caret"
                  onClick={() => toggle(node.token)}
                  type="button"
                >
                  {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                </button>
              ) : (
                <span className="feishu-folder-caret-spacer" />
              )}
              <span className="feishu-folder-icon">
                {isFolder ? <Folder size={14} /> : <FileText size={14} />}
              </span>
              <button
                className="feishu-folder-label"
                disabled={!isFolder}
                onClick={() => isFolder && onSelect(node.token, node.name)}
                title={
                  isFolder
                    ? t('integrations.feishu.picker.selectHint')
                    : t('integrations.feishu.picker.fileHint')
                }
                type="button"
              >
                {node.name}
              </button>
              {isFolder && (
                <button
                  className="feishu-folder-pick"
                  onClick={() => onSelect(node.token, node.name)}
                  type="button"
                >
                  {isSelected
                    ? t('integrations.feishu.picker.selected')
                    : t('integrations.feishu.picker.select')}
                </button>
              )}
            </div>
            {isFolder && isOpen && (
              <FolderLevel
                depth={depth + 1}
                folderToken={node.token}
                onNeedsAuth={onNeedsAuth}
                onSelect={onSelect}
                selectedToken={selectedToken}
              />
            )}
          </li>
        )
      })}
    </ul>
  )
}
