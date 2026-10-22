import { Check, Download, Loader2, ShieldCheck, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useFormatDate } from '../../i18n/formatDate'
import { formatBytes } from './DocumentRow'
import { useDocuments } from './DocumentsContext'
import { downloadDocumentVersion } from './api'
import {
  decideReview,
  decideVersions,
  listReviewDocuments,
  listReviewVersions,
  setUploadReview,
  type ReviewItem,
  type VersionReviewItem,
} from './reviewApi'

function versionKey(v: { document_id: string; version_number: number }): string {
  return `${v.document_id}:${v.version_number}`
}

function messageOf(err: unknown): string {
  return err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
}

/**
 * The upload-approval switch (owner only) and the list of uploads waiting for a decision
 * (owners and admins). Rendered for the space being viewed; nothing for everyone else.
 */
export function UploadReviewSection({
  spaceId,
  role,
  required,
  onChanged,
}: {
  spaceId: string
  role: string | null | undefined
  required: boolean
  onChanged?: () => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [enabled, setEnabled] = useState(required)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const isOwner = role === 'owner'
  const isReviewer = role === 'owner' || role === 'admin'

  useEffect(() => setEnabled(required), [required])

  const toggle = async () => {
    if (!credentials || busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await setUploadReview(credentials, spaceId, !enabled)
      setEnabled(result.enabled)
      onChanged?.()
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }

  if (!isReviewer && !(enabled && role === 'editor')) return null
  return (
    <section className="panel upload-review">
      <div className="panel-heading">
        <div>
          <span className="section-label">
            <ShieldCheck size={14} /> {t('uploadReview.title')}
          </span>
          <p>{t(isReviewer ? 'uploadReview.hintReviewer' : 'uploadReview.hintEditor')}</p>
        </div>
        {isOwner && (
          <label className="upload-review-toggle">
            <input checked={enabled} disabled={busy} onChange={() => void toggle()} type="checkbox" />
            <span>{t('uploadReview.toggle')}</span>
          </label>
        )}
      </div>
      {error && <div className="dialog-error">{error}</div>}
      {isReviewer && <ReviewList spaceId={spaceId} onDecided={onChanged} />}
    </section>
  )
}

function ReviewList({ spaceId, onDecided }: { spaceId: string; onDecided?: () => void }) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const formatDate = useFormatDate()
  const { download, reloadPage, reloadStats } = useDocuments()
  const [tab, setTab] = useState<'pending' | 'rejected'>('pending')
  const [items, setItems] = useState<ReviewItem[]>([])
  // New versions of existing documents waiting for (or refused by) a reviewer, listed beside
  // the new documents and decided the same way.
  const [versionItems, setVersionItems] = useState<VersionReviewItem[]>([])
  const [selectedVersions, setSelectedVersions] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!credentials) return
    setLoading(true)
    try {
      const [list, versions] = await Promise.all([
        listReviewDocuments(credentials, spaceId, tab),
        listReviewVersions(credentials, spaceId, tab),
      ])
      setItems(list.items)
      setVersionItems(versions.items)
      setSelected(new Set())
      setSelectedVersions(new Set())
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setLoading(false)
    }
  }, [credentials, spaceId, tab])

  useEffect(() => {
    void load()
  }, [load])

  const nothingChosen = selected.size === 0 && selectedVersions.size === 0

  const decide = async (action: 'approve' | 'reject') => {
    if (!credentials || busy || nothingChosen) return
    if (action === 'reject' && !note.trim()) {
      setError(t('uploadReview.needReason'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (selected.size > 0) {
        await decideReview(credentials, spaceId, [...selected], action, note.trim())
      }
      if (selectedVersions.size > 0) {
        await decideVersions(
          credentials,
          spaceId,
          versionItems
            .filter((v) => selectedVersions.has(versionKey(v)))
            .map((v) => ({ document_id: v.document_id, version_number: v.version_number })),
          action,
          note.trim(),
        )
      }
      setNote('')
      await Promise.all([load(), reloadPage(), reloadStats()])
      onDecided?.()
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }

  const saveVersion = async (v: VersionReviewItem) => {
    if (!credentials) return
    try {
      const { blob, filename } = await downloadDocumentVersion(
        credentials,
        spaceId,
        v.document_id,
        v.version_number,
      )
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename || v.filename
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch (err) {
      setError(messageOf(err))
    }
  }

  const toggleVersion = (key: string) =>
    setSelectedVersions((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const toggleOne = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <div className="upload-review-list">
      <div className="doc-drawer-tabs" role="tablist">
        {(['pending', 'rejected'] as const).map((s) => (
          <button
            aria-selected={tab === s}
            className={tab === s ? 'active' : ''}
            key={s}
            onClick={() => setTab(s)}
            role="tab"
            type="button"
          >
            {t(`uploadReview.tab.${s}`)}
          </button>
        ))}
      </div>
      {loading && <Loader2 className="spin" size={14} />}
      {!loading && items.length === 0 && versionItems.length === 0 && (
        <div className="state-panel">{t(`uploadReview.empty.${tab}`)}</div>
      )}
      {items.length > 0 && (
        <ul className="space-access-list">
          {items.map((d) => (
            <li className="space-access-row" key={d.id}>
              <label className="space-access-subject">
                <input
                  checked={selected.has(d.id)}
                  onChange={() => toggleOne(d.id)}
                  type="checkbox"
                />
                <span>
                  <strong>{d.name}</strong>
                  <small>
                    {d.uploader_name || t('documents.unknownUploader')} · {formatBytes(d.size_bytes)} ·{' '}
                    {formatDate(d.created_at)}
                    {d.review_note ? ` · ${d.review_note}` : ''}
                  </small>
                </span>
              </label>
              <button
                aria-label={t('documents.download')}
                className="secondary-button icon-button"
                onClick={() => void download(d.id)}
                title={t('uploadReview.openToJudge')}
                type="button"
              >
                <Download size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}
      {versionItems.length > 0 && (
        <>
          <h4 className="upload-review-subhead">{t('uploadReview.versions.heading')}</h4>
          <ul className="space-access-list">
            {versionItems.map((v) => (
              <li className="space-access-row" key={versionKey(v)}>
                <label className="space-access-subject">
                  <input
                    checked={selectedVersions.has(versionKey(v))}
                    onChange={() => toggleVersion(versionKey(v))}
                    type="checkbox"
                  />
                  <span>
                    <strong>
                      {t('uploadReview.versions.line', {
                        name: v.document_name,
                        number: v.version_number,
                      })}
                    </strong>
                    <small>
                      {v.uploader_name || t('documents.unknownUploader')} ·{' '}
                      {formatBytes(v.size_bytes)} · {formatDate(v.created_at)}
                      {v.note ? ` · ${v.note}` : ''}
                      {v.review_note ? ` · ${v.review_note}` : ''}
                    </small>
                  </span>
                </label>
                <button
                  aria-label={t('documents.download')}
                  className="secondary-button icon-button"
                  onClick={() => void saveVersion(v)}
                  title={t('uploadReview.openToJudge')}
                  type="button"
                >
                  <Download size={13} />
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {error && <div className="dialog-error">{error}</div>}
      {(items.length > 0 || versionItems.length > 0) && (
        <div className="upload-review-actions">
          <input
            aria-label={t('uploadReview.note')}
            maxLength={500}
            onChange={(e) => setNote(e.target.value)}
            placeholder={t('uploadReview.notePlaceholder')}
            value={note}
          />
          {tab === 'pending' && (
            <button
              className="secondary-button"
              disabled={busy || nothingChosen}
              onClick={() => void decide('reject')}
              type="button"
            >
              <X size={13} /> {t('uploadReview.reject')}
            </button>
          )}
          <button
            className="primary-button"
            disabled={busy || nothingChosen}
            onClick={() => void decide('approve')}
            type="button"
          >
            {busy ? <Loader2 className="spin" size={13} /> : <Check size={13} />}
            {t('uploadReview.approve')}
          </button>
        </div>
      )}
    </div>
  )
}
