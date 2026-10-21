import { Ban, CheckCircle2, Info, Loader2, MinusCircle, SearchCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { useMembers } from '../members/MembersContext'
import { checkDocumentAccess, type AccessCheck, type AccessCheckStep } from './accessRulesApi'

/**
 * "Why can (or can't) this person read this document?" for a space manager.
 *
 * The verdict and the reasons come from the server, which works them out with the same code
 * that decides real reads. This screen only words them. Every member is offered, including
 * suspended ones: asking about a deactivated account is a normal thing to want to know.
 *
 * ``rulesVersion`` changes whenever the rules do, so an answer shown for the old rules is
 * dropped instead of left on screen looking current.
 */
export function AccessCheckSection({
  spaceId,
  documentId,
  rulesVersion,
}: {
  spaceId: string
  documentId: string
  rulesVersion: string
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const { state: members } = useMembers()
  const [userId, setUserId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AccessCheck | null>(null)
  const items = members.kind === 'ready' ? members.items : []

  useEffect(() => {
    setResult(null)
    setError(null)
  }, [rulesVersion, userId])

  useEffect(() => {
    if (!userId && items.length > 0) setUserId(items[0].id)
  }, [items, userId])

  const run = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!credentials || !userId || busy) return
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult(await checkDocumentAccess(credentials, spaceId, documentId, userId))
    } catch (err) {
      setError(err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="doc-access-check">
      <h3>
        <SearchCheck size={13} /> {t('docAccess.check.title')}
      </h3>
      <small className="doc-access-check-hint">{t('docAccess.check.hint')}</small>
      <form className="doc-access-check-form" onSubmit={(e) => void run(e)}>
        <select
          aria-label={t('docAccess.check.person')}
          onChange={(e) => setUserId(e.target.value)}
          value={userId}
        >
          {items.map((m) => (
            <option key={m.id} value={m.id}>
              {m.display_name}
              {m.email ? ` · ${m.email}` : ''}
              {m.status === 'suspended' || m.status === 'archived'
                ? ` · ${t('docAccess.check.inactive')}`
                : ''}
            </option>
          ))}
        </select>
        <button className="secondary-button" disabled={!userId || busy} type="submit">
          {busy ? <Loader2 className="spin" size={14} /> : null}
          <span>{t('docAccess.check.run')}</span>
        </button>
      </form>
      {error && <div className="dialog-error">{error}</div>}
      {result && <Verdict result={result} />}
    </section>
  )
}

function Verdict({ result }: { result: AccessCheck }) {
  const { t } = useTranslation()
  const name = result.user_display_name
  return (
    <div className="doc-access-verdict" data-can-read={result.can_read ? 'yes' : 'no'}>
      <p className="doc-access-verdict-head">
        {result.can_read ? <CheckCircle2 size={15} /> : <Ban size={15} />}
        <strong>{t(`docAccess.check.outcome.${result.outcome}`, { name })}</strong>
      </p>
      <small>
        {t(result.appears_in_search ? 'docAccess.check.searchYes' : 'docAccess.check.searchNo')}
      </small>
      <ul>
        {result.steps.map((step, i) => (
          <li className={`doc-access-step doc-access-step-${step.effect}`} key={`${step.code}-${i}`}>
            <StepIcon effect={step.effect} />
            <span>{stepText(t, step)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function StepIcon({ effect }: { effect: AccessCheckStep['effect'] }) {
  if (effect === 'grants') return <CheckCircle2 size={13} />
  if (effect === 'blocks') return <Ban size={13} />
  if (effect === 'ignored') return <MinusCircle size={13} />
  return <Info size={13} />
}

/** Our own wording for a step, falling back to the server's sentence for a code we do not know. */
function stepText(t: (key: string, options?: Record<string, unknown>) => string, step: AccessCheckStep) {
  const via = step.hit_path
    ? t(`docAccess.check.via.${step.hit_path}`, { defaultValue: '' })
    : ''
  const type = step.subject_type
    ? t(`spaceAccess.subjectTypeShort.${step.subject_type}`, { defaultValue: '' })
    : ''
  return t(`docAccess.check.step.${step.code}`, {
    defaultValue: step.summary,
    name: step.subject_display_name ?? '',
    role: step.role_code ?? '',
    via: via ? ` (${via})` : '',
    type,
  })
}
