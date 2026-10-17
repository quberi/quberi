import { FileText, Loader2, Search, ShieldCheck, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { formatBytes } from './DocumentRow'
import { DocumentAccessTab } from './DocumentAccessTab'
import { listManagedDocuments, type ManagedDocument } from './accessRulesApi'

const PAGE = 20

/**
 * For whoever manages an enterprise space but may not read it: find a document by name and
 * set who can read it. Shows names, sizes and how many exceptions each has, never content.
 */
export function SpaceDocumentAccessPanel({ spaceId }: { spaceId: string }) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [q, setQ] = useState('')
  const [offset, setOffset] = useState(0)
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'error'; message: string }
    | { kind: 'ready'; items: ManagedDocument[]; total: number }
  >({ kind: 'loading' })
  const [open, setOpen] = useState<ManagedDocument | null>(null)

  const load = useCallback(async () => {
    if (!credentials) return
    setState({ kind: 'loading' })
    try {
      const page = await listManagedDocuments(credentials, spaceId, { q, offset, limit: PAGE })
      setState({ kind: 'ready', items: page.items, total: page.total })
    } catch (err) {
      setState({
        kind: 'error',
        message: err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err),
      })
    }
  }, [credentials, spaceId, q, offset])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <span className="section-label">
            <ShieldCheck size={14} /> {t('docAccessAdmin.title')}
          </span>
          <p>{t('docAccessAdmin.hint')}</p>
        </div>
      </div>
      <label className="doc-access-search">
        <Search size={14} aria-hidden="true" />
        <input
          onChange={(e) => {
            setOffset(0)
            setQ(e.target.value)
          }}
          placeholder={t('docAccessAdmin.search')}
          value={q}
        />
      </label>
      {state.kind === 'loading' && <div className="state-panel"><Loader2 className="spin" size={14} /></div>}
      {state.kind === 'error' && <div className="state-panel error-state">{state.message}</div>}
      {state.kind === 'ready' && state.items.length === 0 && (
        <div className="state-panel">{t('docAccessAdmin.empty')}</div>
      )}
      {state.kind === 'ready' && state.items.length > 0 && (
        <>
          <ul className="space-access-list">
            {state.items.map((d) => (
              <li className="space-access-row" key={d.id}>
                <span className="space-access-subject">
                  <FileText size={14} aria-hidden="true" />
                  <span>
                    <strong>{d.name}</strong>
                    <small>
                      {d.mime_type} · {formatBytes(d.size_bytes)}
                    </small>
                  </span>
                </span>
                {d.allow_rules > 0 && (
                  <span className="badge role-viewer">
                    {t('docAccessAdmin.allowCount', { count: d.allow_rules })}
                  </span>
                )}
                {d.deny_rules > 0 && (
                  <span className="badge badge-pending">
                    {t('docAccessAdmin.denyCount', { count: d.deny_rules })}
                  </span>
                )}
                <button className="secondary-button" onClick={() => setOpen(d)} type="button">
                  {t('docAccessAdmin.setAccess')}
                </button>
              </li>
            ))}
          </ul>
          {state.total > PAGE && (
            <div className="doc-access-pager">
              <button
                className="secondary-button"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE))}
                type="button"
              >
                {t('docAccessAdmin.prev')}
              </button>
              <small>
                {offset + 1}–{Math.min(offset + PAGE, state.total)} / {state.total}
              </small>
              <button
                className="secondary-button"
                disabled={offset + PAGE >= state.total}
                onClick={() => setOffset(offset + PAGE)}
                type="button"
              >
                {t('docAccessAdmin.next')}
              </button>
            </div>
          )}
        </>
      )}
      {open && (
        <div className="doc-drawer-backdrop" onClick={() => { setOpen(null); void load() }} role="presentation">
          <aside
            aria-modal="true"
            className="doc-drawer"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
          >
            <header className="doc-drawer-head">
              <div className="doc-drawer-title">
                <FileText size={16} />
                <div>
                  <strong>{open.name}</strong>
                  <small>{open.mime_type}</small>
                </div>
              </div>
              <button
                aria-label={t('knowledge.detail.close')}
                className="icon-button"
                onClick={() => { setOpen(null); void load() }}
                type="button"
              >
                <X size={16} />
              </button>
            </header>
            <DocumentAccessTab documentId={open.id} spaceId={spaceId} />
          </aside>
        </div>
      )}
    </section>
  )
}
