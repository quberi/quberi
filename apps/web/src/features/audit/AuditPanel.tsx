import { ChevronDown, ChevronRight, Download, Loader2, RefreshCw } from 'lucide-react'
import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useFormatDate } from '../../i18n/formatDate'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { listMembers } from '../members/api'
import type { MemberSummary } from '../members/types'
import { listOrganizationSpaces } from '../spaces/api'
import type { SpaceSummary } from '../spaces/types'

import { exportAuditCsv, listAuditActions, searchAuditEvents } from './api'
import { eventSentence } from './sentence'
import type { AuditFilters, RecentEvent } from './types'

type RangeKey = 'all' | '1h' | '24h' | '7d' | '30d'

const RANGE_MS: Record<RangeKey, number | null> = {
  all: null,
  '1h': 3_600_000,
  '24h': 86_400_000,
  '7d': 7 * 86_400_000,
  '30d': 30 * 86_400_000,
}
const RANGES = Object.keys(RANGE_MS) as RangeKey[]
const PAGE_SIZE = 50

function toMessage(error: unknown): string {
  return error instanceof ApiError ? error.detail || `HTTP ${error.status}` : String(error)
}

/**
 * Admin-console audit page: a searchable, paged log of what happened in the
 * organisation, including which documents the assistant could see for each
 * question. Filters narrow the log; "load more" follows a cursor, so events
 * arriving while someone reads never shift or repeat rows.
 */
export function AuditPanel() {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const { credentials } = useAuth()

  const [range, setRange] = useState<RangeKey>('7d')
  const [action, setAction] = useState('')
  const [actorId, setActorId] = useState('')
  const [spaceId, setSpaceId] = useState('')

  const [items, setItems] = useState<RecentEvent[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [exporting, setExporting] = useState(false)

  const [actions, setActions] = useState<string[]>([])
  const [members, setMembers] = useState<MemberSummary[]>([])
  const [spaces, setSpaces] = useState<SpaceSummary[]>([])

  // Only the newest request may write results, so a slow answer to an old
  // filter cannot overwrite the page the user is now looking at.
  const requestId = useRef(0)

  const buildFilters = useCallback((): AuditFilters => {
    const ms = RANGE_MS[range]
    return {
      action: action || undefined,
      actorId: actorId || undefined,
      spaceId: spaceId || undefined,
      since: ms === null ? undefined : new Date(Date.now() - ms).toISOString(),
    }
  }, [range, action, actorId, spaceId])

  const load = useCallback(
    async (more: boolean) => {
      if (!credentials) return
      const id = ++requestId.current
      setLoading(true)
      setError(null)
      try {
        const page = await searchAuditEvents(credentials, buildFilters(), {
          cursor: more ? cursor : null,
          limit: PAGE_SIZE,
        })
        if (id !== requestId.current) return
        setItems((prev) => (more ? [...prev, ...page.items] : page.items))
        setCursor(page.next_cursor)
        if (!more) setOpen(new Set())
        setLoaded(true)
      } catch (err) {
        if (id !== requestId.current) return
        setError(toMessage(err))
      } finally {
        if (id === requestId.current) setLoading(false)
      }
    },
    // ``cursor`` is read only when loading more; filters rebuild the query.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [credentials, buildFilters, cursor],
  )

  // Reload from the top whenever a filter changes (and on first show).
  useEffect(() => {
    void load(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credentials, range, action, actorId, spaceId])

  // Choices for the dropdowns. A failure just leaves a dropdown empty.
  useEffect(() => {
    if (!credentials) return
    void listAuditActions(credentials)
      .then((r) => setActions(r.items))
      .catch(() => undefined)
    void listMembers(credentials)
      .then((r) => setMembers(r.items))
      .catch(() => undefined)
    void listOrganizationSpaces(credentials)
      .then((r) => setSpaces(r.items))
      .catch(() => undefined)
  }, [credentials])

  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const doExport = async () => {
    if (!credentials) return
    setExporting(true)
    setError(null)
    try {
      const blob = await exportAuditCsv(credentials, buildFilters())
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'audit-log.csv'
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setError(toMessage(err))
    } finally {
      setExporting(false)
    }
  }

  const actionLabel = (code: string) =>
    t(`audit.page.actionName.${code}`, { defaultValue: code })

  return (
    <section className="panel audit-panel">
      <div className="panel-heading">
        <div>
          <span className="section-label">{t('audit.page.title')}</span>
          <p>{t('audit.page.description')}</p>
        </div>
        <div className="audit-heading-actions">
          <button
            className="secondary-button"
            disabled={loading}
            onClick={() => void load(false)}
            type="button"
          >
            <RefreshCw size={13} className={loading ? 'spin' : undefined} />
            <span>{t('audit.recent.refresh')}</span>
          </button>
          <button
            className="secondary-button"
            disabled={exporting}
            onClick={() => void doExport()}
            type="button"
          >
            {exporting ? <Loader2 size={13} className="spin" /> : <Download size={13} />}
            <span>{t('audit.page.export')}</span>
          </button>
        </div>
      </div>

      <div className="audit-filters">
        <label className="space-view-filter">
          <span>{t('audit.page.filter.time')}</span>
          <select onChange={(e) => setRange(e.target.value as RangeKey)} value={range}>
            {RANGES.map((key) => (
              <option key={key} value={key}>
                {t(`audit.page.range.${key}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="space-view-filter">
          <span>{t('audit.page.filter.action')}</span>
          <select onChange={(e) => setAction(e.target.value)} value={action}>
            <option value="">{t('audit.page.filter.any')}</option>
            {actions.map((code) => (
              <option key={code} value={code}>
                {actionLabel(code)}
              </option>
            ))}
          </select>
        </label>
        <label className="space-view-filter">
          <span>{t('audit.page.filter.person')}</span>
          <select onChange={(e) => setActorId(e.target.value)} value={actorId}>
            <option value="">{t('audit.page.filter.any')}</option>
            {members.map((m) => (
              <option key={m.id} value={m.id}>
                {m.display_name || m.email}
              </option>
            ))}
          </select>
        </label>
        <label className="space-view-filter">
          <span>{t('audit.page.filter.space')}</span>
          <select onChange={(e) => setSpaceId(e.target.value)} value={spaceId}>
            <option value="">{t('audit.page.filter.any')}</option>
            {spaces.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && <div className="state-panel error-state">{error}</div>}

      {!error && loaded && items.length === 0 && (
        <div className="state-panel">{t('audit.page.empty')}</div>
      )}

      {items.length > 0 && (
        <table className="space-view-table audit-table">
          <thead>
            <tr>
              <th aria-label={t('audit.page.details')} />
              <th>{t('audit.page.col.time')}</th>
              <th>{t('audit.page.col.event')}</th>
              <th>{t('audit.page.col.space')}</th>
            </tr>
          </thead>
          <tbody>
            {items.map((ev) => (
              <Fragment key={ev.id}>
                <tr onClick={() => toggle(ev.id)}>
                  <td className="audit-caret">
                    {open.has(ev.id) ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  </td>
                  <td className="audit-time">{formatDate(ev.created_at)}</td>
                  <td>{eventSentence(t, ev)}</td>
                  <td>{ev.space_display_name || '—'}</td>
                </tr>
                {open.has(ev.id) && (
                  <tr className="audit-detail-row">
                    <td />
                    <td colSpan={3}>
                      <EventDetails event={ev} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}

      {cursor && (
        <div className="audit-more">
          <button
            className="secondary-button"
            disabled={loading}
            onClick={() => void load(true)}
            type="button"
          >
            {loading ? <Loader2 size={13} className="spin" /> : null}
            <span>{t('audit.page.loadMore')}</span>
          </button>
        </div>
      )}
    </section>
  )
}

function EventDetails({ event }: { event: RecentEvent }) {
  const { t } = useTranslation()
  const docs = event.retrieved_documents ?? []
  // The retrieved ids are shown as names above; keep the raw rest as JSON.
  const { retrieved_document_ids: _ids, ...rest } = event.payload as Record<string, unknown>
  void _ids
  return (
    <div className="audit-details">
      {event.action === 'message.sent' && (
        <div className="audit-details-block">
          <strong>{t('audit.page.retrievedTitle')}</strong>
          {docs.length === 0 ? (
            <p>{t('audit.page.retrievedNone')}</p>
          ) : (
            <ul>
              {docs.map((d) => (
                <li key={d.id}>
                  {d.name ?? <em>{t('audit.page.deletedDocument')}</em>}
                </li>
              ))}
            </ul>
          )}
          <small>{t('audit.page.retrievedHint')}</small>
        </div>
      )}
      <div className="audit-details-block">
        <strong>{t('audit.page.rawTitle')}</strong>
        <pre>{JSON.stringify(rest, null, 2)}</pre>
      </div>
    </div>
  )
}
