import {
  Cloud,
  FolderInput,
  Link2,
  Plug,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import {
  deleteSource as apiDeleteSource,
  listSources as apiListSources,
  registerSource as apiRegisterSource,
  syncSource as apiSyncSource,
} from '../documents/api'
import type { DocumentSource } from '../documents/types'
import { useSpaces } from '../spaces/SpacesContext'

import { AddFeishuDialog } from './AddFeishuDialog'
import { ExternalToolsCard } from '../oauth/ExternalToolsCard'
import { useFormatDate } from '../../i18n/formatDate'

/**
 * IntegrationsPanel — org-admin surface for wiring external document
 * sources into Quberi. Today this is Feishu cloud-docs (reference-only
 * sync); OSS / S3 stubs are listed but greyed until implemented.
 *
 * The panel is deliberately self-contained (its own load/mutate state)
 * because integrations are an infrequent, admin-only concern — no need
 * to hoist a context provider for it.
 */

type LoadState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: DocumentSource[] }

function toMessage(err: unknown): string {
  return err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
}

export function IntegrationsPanel() {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [state, setState] = useState<LoadState>({ kind: 'idle' })
  const [addOpen, setAddOpen] = useState(false)

  const reload = useCallback(async () => {
    if (!credentials) return
    setState({ kind: 'loading' })
    try {
      const { items } = await apiListSources(credentials)
      setState({ kind: 'ready', items })
    } catch (err) {
      setState({ kind: 'error', message: toMessage(err) })
    }
  }, [credentials])

  useEffect(() => {
    void reload()
  }, [reload])

  const feishuSources =
    state.kind === 'ready'
      ? state.items.filter((s) => s.kind === 'feishu')
      : []

  return (
    <>
      <section className="panel integrations-header">
        <div className="integrations-header-lockup">
          <span className="integrations-header-mark">
            <Plug size={18} />
          </span>
          <div>
            <span className="section-label">
              <Link2 size={14} /> {t('integrations.eyebrow')}
            </span>
            <h1>{t('integrations.title')}</h1>
            <p>{t('integrations.description')}</p>
          </div>
          <button
            className="primary-button"
            onClick={() => setAddOpen(true)}
            type="button"
          >
            <Plus size={15} />
            <span>{t('integrations.addFeishu')}</span>
          </button>
        </div>
      </section>

      {state.kind === 'loading' && (
        <section className="panel">
          <div className="state-panel">{t('integrations.loading')}</div>
        </section>
      )}
      {state.kind === 'error' && (
        <section className="panel">
          <div className="state-panel error-state">
            {t('integrations.loadError')}: {state.message}
          </div>
        </section>
      )}

      {state.kind === 'ready' && (
        <section className="panel">
          <div className="panel-heading">
            <div>
              <span className="section-label">
                <Cloud size={15} /> {t('integrations.feishu.title')}
              </span>
              <p>{t('integrations.feishu.subtitle')}</p>
            </div>
            <button
              className="secondary-button icon-button"
              onClick={() => void reload()}
              type="button"
            >
              <RefreshCw size={13} />
              <span>{t('integrations.refresh')}</span>
            </button>
          </div>

          {feishuSources.length === 0 && (
            <div className="state-panel">{t('integrations.feishu.empty')}</div>
          )}

          {feishuSources.length > 0 && (
            <ul className="integration-source-list">
              {feishuSources.map((s) => (
                <SourceRow key={s.id} source={s} onChanged={reload} />
              ))}
            </ul>
          )}
        </section>
      )}

      <ExternalToolsCard />

      {addOpen && (
        <AddFeishuDialog
          onClose={() => setAddOpen(false)}
          onCreated={async () => {
            setAddOpen(false)
            await reload()
          }}
        />
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// One source row
// ---------------------------------------------------------------------------

function SourceRow({
  source,
  onChanged,
}: {
  source: DocumentSource
  onChanged: () => Promise<void>
}) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const { credentials } = useAuth()
  const { state: spacesState } = useSpaces()
  const [busy, setBusy] = useState<null | 'sync' | 'delete'>(null)
  const [result, setResult] = useState<string | null>(null)

  const folderToken = String(
    (source.config as Record<string, unknown>).folder_token ?? '',
  )
  const targetSpaceId = String(
    (source.config as Record<string, unknown>).target_space_id ?? '',
  )
  const spaceName =
    spacesState.kind === 'ready'
      ? spacesState.items.find((s) => s.id === targetSpaceId)?.name ??
        targetSpaceId.slice(0, 8)
      : targetSpaceId.slice(0, 8)

  const doSync = async () => {
    if (!credentials) return
    setBusy('sync')
    setResult(null)
    try {
      const r = await apiSyncSource(credentials, source.id)
      setResult(
        r.ok
          ? t('integrations.feishu.syncOk')
          : t('integrations.feishu.syncFail', { message: r.message }),
      )
      await onChanged()
    } catch (err) {
      setResult(t('integrations.feishu.syncFail', { message: toMessage(err) }))
    } finally {
      setBusy(null)
    }
  }

  const doDelete = async () => {
    if (!credentials) return
    if (!window.confirm(t('integrations.feishu.confirmDelete', { name: source.name })))
      return
    setBusy('delete')
    try {
      await apiDeleteSource(credentials, source.id)
      await onChanged()
    } catch (err) {
      window.alert(toMessage(err))
      setBusy(null)
    }
  }

  return (
    <li className="integration-source-row">
      <span className="integration-source-mark" aria-hidden="true">
        <Cloud size={16} />
      </span>
      <div className="integration-source-body">
        <strong>{source.name}</strong>
        <small className="integration-source-meta">
          <span className="chip chip-inline">
            <FolderInput size={10} /> {folderToken.slice(0, 14) || '—'}
          </span>
          <span className="dot-sep">·</span>
          <span>{t('integrations.feishu.targetSpace', { space: spaceName })}</span>
          <span className="dot-sep">·</span>
          <span>
            {source.last_synced_at
              ? t('integrations.feishu.lastSynced', {
                  when: formatDate(source.last_synced_at),
                })
              : t('integrations.feishu.neverSynced')}
          </span>
          <span
            className={`chip chip-inline chip-cache-${source.caching_policy}`}
          >
            {t(`integrations.cachingPolicy.${source.caching_policy}`)}
          </span>
        </small>
        {result && <div className="integration-source-result">{result}</div>}
      </div>
      <div className="integration-source-actions">
        <button
          className="secondary-button icon-button"
          disabled={busy !== null}
          onClick={() => void doSync()}
          type="button"
        >
          <RefreshCw size={13} className={busy === 'sync' ? 'spin' : undefined} />
          <span>{t('integrations.feishu.sync')}</span>
        </button>
        <button
          className="icon-button danger"
          disabled={busy !== null}
          onClick={() => void doDelete()}
          title={t('integrations.feishu.delete')}
          type="button"
        >
          <Trash2 size={13} />
        </button>
      </div>
    </li>
  )
}
