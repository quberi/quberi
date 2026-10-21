import { Ban, Loader2, Share2, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import type { AccessSubjectType, DocumentAccessSummary } from '../spaceAccess/types'
import { AccessCheckSection } from './AccessCheckSection'
import { DocumentAccessSummaryCard } from './DocumentAccessSummaryCard'
import { subjectIdForApi, useSubjectCandidates } from '../spaceAccess/useSubjectCandidates'
import {
  addAccessRule,
  getDocumentAccessSummary,
  listAccessRules,
  removeAccessRule,
  type AccessEffect,
  type DocumentAccessRule,
} from './accessRulesApi'

const SUBJECT_TYPES: AccessSubjectType[] = [
  'user',
  'group',
  'organization_unit',
  'position',
  'organization',
]

type LoadState =
  | { kind: 'loading' }
  | { kind: 'forbidden' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; items: DocumentAccessRule[]; summary: DocumentAccessSummary | null }

function messageOf(err: unknown): string {
  return err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)
}

/**
 * Who this one document is shared with, and who it is hidden from.
 *
 * A document is readable by everyone who can read its space, plus the people it is shared
 * with, minus the people it is hidden from. Hiding wins over everything. Only a space admin
 * or owner can see or change this; everyone else is told so. Changes apply immediately.
 */
export function DocumentAccessTab({
  spaceId,
  documentId,
}: {
  spaceId: string
  documentId: string
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [state, setState] = useState<LoadState>({ kind: 'loading' })
  const [effect, setEffect] = useState<AccessEffect>('allow')
  const [subjectType, setSubjectType] = useState<AccessSubjectType>('user')
  const [subjectId, setSubjectId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const allCandidates = useSubjectCandidates(subjectType)
  const rules = state.kind === 'ready' ? state.items : []
  // Offer only people who are not already covered: not in the list being added to, and for an
  // Allow not already denied (a denial wins, so allowing them would do nothing).
  const candidates = useMemo(
    () =>
      allCandidates.filter(
        (c) =>
          !rules.some(
            (r) =>
              r.subject_type === subjectType &&
              (subjectType === 'organization' || r.subject_id === c.id) &&
              (r.effect === effect || (effect === 'allow' && r.effect === 'deny')),
          ),
      ),
    [allCandidates, rules, subjectType, effect],
  )
  const load = useCallback(async () => {
    if (!credentials) return
    setState({ kind: 'loading' })
    try {
      // The numbers are a convenience: if they cannot be had, the rules still show.
      const [list, summary] = await Promise.all([
        listAccessRules(credentials, spaceId, documentId),
        getDocumentAccessSummary(credentials, spaceId, documentId).catch(() => null),
      ])
      setState({ kind: 'ready', items: list.items, summary })
    } catch (err) {
      if (err instanceof ApiError && (err.status === 403 || err.status === 404)) {
        setState({ kind: 'forbidden' })
      } else {
        setState({ kind: 'error', message: messageOf(err) })
      }
    }
  }, [credentials, spaceId, documentId])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    setSubjectId(candidates[0]?.id ?? '')
  }, [subjectType, candidates])

  if (state.kind === 'loading') {
    return (
      <div className="doc-drawer-body">
        <Loader2 className="spin" size={16} />
      </div>
    )
  }
  if (state.kind === 'forbidden') {
    return (
      <div className="doc-drawer-body">
        <p className="doc-drawer-note">{t('docAccess.forbidden')}</p>
      </div>
    )
  }
  if (state.kind === 'error') {
    return (
      <div className="doc-drawer-body">
        <div className="dialog-error">{state.message}</div>
        <button className="secondary-button" onClick={() => void load()} type="button">
          {t('docAccess.retry')}
        </button>
      </div>
    )
  }

  const shared = rules.filter((r) => r.effect === 'allow')
  const hidden = rules.filter((r) => r.effect === 'deny')

  const add = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!credentials || !subjectId || busy) return
    setBusy(true)
    setError(null)
    try {
      await addAccessRule(credentials, spaceId, documentId, {
        effect,
        subject_type: subjectType,
        subject_id: subjectIdForApi(subjectType, subjectId),
      })
      await load()
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (rule: DocumentAccessRule) => {
    if (!credentials || busy) return
    setBusy(true)
    setError(null)
    try {
      await removeAccessRule(credentials, spaceId, documentId, rule.id)
      await load()
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="doc-drawer-body doc-access">
      {state.summary && <DocumentAccessSummaryCard summary={state.summary} />}
      <p className="doc-drawer-note">{t('docAccess.intro')}</p>
      <RuleList
        empty={t('docAccess.noneShared')}
        icon={<Share2 size={13} />}
        onRemove={remove}
        rules={shared}
        title={t('docAccess.sharedWith')}
      />
      <RuleList
        empty={t('docAccess.noneHidden')}
        icon={<Ban size={13} />}
        onRemove={remove}
        rules={hidden}
        title={t('docAccess.hiddenFrom')}
      />
      <form className="doc-access-form dialog-form" onSubmit={(e) => void add(e)}>
        <div className="doc-access-effects" role="radiogroup" aria-label={t('docAccess.effect')}>
          {(['allow', 'deny'] as AccessEffect[]).map((e) => (
            <button
              aria-checked={effect === e}
              className={`doc-access-effect doc-access-effect-${e}${effect === e ? ' active' : ''}`}
              key={e}
              onClick={() => setEffect(e)}
              role="radio"
              type="button"
            >
              <strong>{t(`docAccess.effectName.${e}`)}</strong>
              <small>{t(`docAccess.effectHint.${e}`)}</small>
            </button>
          ))}
        </div>
        <label>
          <span>{t('spaceAccess.subjectType')}</span>
          <select
            onChange={(e) => setSubjectType(e.target.value as AccessSubjectType)}
            value={subjectType}
          >
            {SUBJECT_TYPES.map((s) => (
              <option key={s} value={s}>
                {t(`spaceAccess.subjectTypeOption.${s}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>{t('spaceAccess.subject')}</span>
          <select onChange={(e) => setSubjectId(e.target.value)} value={subjectId}>
            {candidates.length === 0 && (
              <option value="">{t('docAccess.allCovered')}</option>
            )}
            {candidates.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
                {c.subtitle ? ` · ${c.subtitle}` : ''}
              </option>
            ))}
          </select>
        </label>
        {error && <div className="dialog-error">{error}</div>}
        <button className="primary-button" disabled={!subjectId || busy} type="submit">
          {busy ? <Loader2 className="spin" size={14} /> : null}
          <span>{t('docAccess.add')}</span>
        </button>
      </form>
      <AccessCheckSection
        documentId={documentId}
        rulesVersion={rules.map((r) => r.id).join(',')}
        spaceId={spaceId}
      />
    </div>
  )
}

function RuleList({
  title,
  icon,
  empty,
  rules,
  onRemove,
}: {
  title: string
  icon: React.ReactNode
  empty: string
  rules: DocumentAccessRule[]
  onRemove: (rule: DocumentAccessRule) => void
}) {
  const { t } = useTranslation()
  return (
    <section className="doc-access-list">
      <h3>
        {icon} {title}
      </h3>
      {rules.length === 0 ? (
        <small>{empty}</small>
      ) : (
        <ul>
          {rules.map((r) => (
            <li key={r.id}>
              <span>
                <strong>{r.subject_display_name}</strong>
                <small>
                  {t(`spaceAccess.subjectTypeShort.${r.subject_type}`)}
                  {r.subject_subtitle ? ` · ${r.subject_subtitle}` : ''}
                </small>
              </span>
              <button
                aria-label={t('docAccess.remove')}
                className="icon-button"
                onClick={() => onRemove(r)}
                type="button"
              >
                <Trash2 size={14} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
