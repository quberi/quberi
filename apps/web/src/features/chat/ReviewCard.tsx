import { Loader2, Send, ShieldAlert, ShieldCheck, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { ReviewEditsBody, ReviewPart, ReviewPayload, ReviewSpan } from './api'

type Kind = ReviewSpan['kind']

interface RefState {
  index: number
  include: boolean
  text: string
}

/** What the card edits: the exact text of every piece that would be sent. */
interface Draft {
  question: string
  history: string[]
  refs: RefState[]
}

type PartKey = 'q' | `h${number}` | `r${number}`

const PREVIEW_DELAY_MS = 400

function initialDraft(review: ReviewPayload): Draft {
  return {
    question: review.question.text,
    history: review.history.map((h) => h.text),
    refs: review.references.map((r) => ({
      index: r.index,
      include: r.included,
      text: r.text,
    })),
  }
}

function toBody(draft: Draft): ReviewEditsBody {
  return {
    question: draft.question,
    history: draft.history,
    references: draft.refs.map((r) => ({
      index: r.index,
      include: r.include,
      text: r.text,
    })),
  }
}

/** Cut ``text`` into plain and highlighted runs. */
function segments(text: string, spans: ReviewSpan[]) {
  const out: { text: string; span?: ReviewSpan }[] = []
  let cursor = 0
  for (const span of [...spans].sort((a, b) => a.start - b.start)) {
    if (span.start < cursor || span.end > text.length) continue
    if (span.start > cursor) out.push({ text: text.slice(cursor, span.start) })
    out.push({ text: text.slice(span.start, span.end), span })
    cursor = span.end
  }
  if (cursor < text.length) out.push({ text: text.slice(cursor) })
  return out
}

/**
 * "About to be sent": the prompt held for review, shown in the thread like a
 * thinking step. Suspected sensitive data is highlighted; the person can
 * delete or replace it, edit any piece freely, drop a reference passage, then
 * send or cancel. Highlights always describe the text as it is *now*: after an
 * edit the server scans the new text again.
 */
export function ReviewCard({
  review,
  error,
  busy,
  onApprove,
  onCancel,
  onPreview,
}: {
  review: ReviewPayload
  error: string | null
  busy: boolean
  onApprove: (edits: ReviewEditsBody) => void
  onCancel: () => void
  onPreview: (edits: ReviewEditsBody, signal?: AbortSignal) => Promise<ReviewPayload>
}) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState<Draft>(() => initialDraft(review))
  // The latest server scan; its spans are valid only for the text it scanned.
  const [scan, setScan] = useState<ReviewPayload>(review)
  const [scanFailed, setScanFailed] = useState(false)
  const [editing, setEditing] = useState<Set<PartKey>>(new Set())
  const [active, setActive] = useState<{ part: PartKey; start: number } | null>(null)
  const [replacement, setReplacement] = useState('')
  const [showRaw, setShowRaw] = useState(false)
  const first = useRef(true)

  // Re-scan (and rebuild the exact outgoing messages) shortly after edits stop.
  useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      onPreview(toBody(draft), controller.signal)
        .then((next) => {
          if (controller.signal.aborted) return
          setScan(next)
          setScanFailed(false)
        })
        .catch(() => {
          if (!controller.signal.aborted) setScanFailed(true)
        })
    }, PREVIEW_DELAY_MS)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [draft, onPreview])

  const kindLabel = (kind: Kind) => t(`chat.review.kind.${kind}`)

  /** The spans for a piece, only if they were computed for exactly this text. */
  const spansFor = (key: PartKey, text: string): ReviewSpan[] => {
    let part: ReviewPart | undefined
    if (key === 'q') part = scan.question
    else if (key.startsWith('h')) part = scan.history[Number(key.slice(1))]
    else part = scan.references[Number(key.slice(1))]
    return part && part.text === text ? part.spans : []
  }

  const setPartText = useCallback((key: PartKey, text: string) => {
    setDraft((d) => {
      if (key === 'q') return { ...d, question: text }
      if (key.startsWith('h')) {
        const i = Number(key.slice(1))
        return { ...d, history: d.history.map((h, n) => (n === i ? text : h)) }
      }
      const i = Number(key.slice(1))
      return { ...d, refs: d.refs.map((r, n) => (n === i ? { ...r, text } : r)) }
    })
  }, [])

  const textOf = (key: PartKey): string => {
    if (key === 'q') return draft.question
    if (key.startsWith('h')) return draft.history[Number(key.slice(1))] ?? ''
    return draft.refs[Number(key.slice(1))]?.text ?? ''
  }

  const applyToSpan = (key: PartKey, span: ReviewSpan, value: string) => {
    const text = textOf(key)
    setPartText(key, text.slice(0, span.start) + value + text.slice(span.end))
    setActive(null)
  }

  const allKeys: PartKey[] = [
    'q',
    ...draft.history.map((_, i) => `h${i}` as PartKey),
    ...draft.refs.flatMap((r, i) => (r.include ? [`r${i}` as PartKey] : [])),
  ]
  const flagged = allKeys.reduce((n, k) => n + spansFor(k, textOf(k)).length, 0)

  const redactAll = (mode: 'delete' | 'placeholder') => {
    setDraft((d) => {
      const next: Draft = { ...d, history: [...d.history], refs: [...d.refs] }
      for (const key of allKeys) {
        const text = textOf(key)
        const spans = spansFor(key, text)
        if (!spans.length) continue
        let out = text
        for (const s of [...spans].sort((a, b) => b.start - a.start)) {
          out =
            out.slice(0, s.start) +
            (mode === 'delete' ? '' : `[${kindLabel(s.kind)}]`) +
            out.slice(s.end)
        }
        if (key === 'q') next.question = out
        else if (key.startsWith('h')) next.history[Number(key.slice(1))] = out
        else {
          const i = Number(key.slice(1))
          next.refs[i] = { ...next.refs[i], text: out }
        }
      }
      return next
    })
    setActive(null)
  }

  const hadReferences = review.references.length > 0
  const noneIncluded = hadReferences && draft.refs.every((r) => !r.include)
  const canSend = !busy && draft.question.trim().length > 0 && !noneIncluded
  const changed =
    JSON.stringify(toBody(draft)) !== JSON.stringify(toBody(initialDraft(review)))

  const model = review.model
  const destination = model
    ? model.vendor
      ? t('chat.review.toVendor', { model: model.display_name, vendor: model.vendor })
      : t('chat.review.toLocal', { model: model.display_name })
    : t('chat.review.toModel')

  const renderPart = (key: PartKey, text: string, opts: { rows?: number } = {}) => {
    const isEditing = editing.has(key)
    const spans = spansFor(key, text)
    return (
      <div className="review-part-body">
        {isEditing ? (
          <textarea
            aria-label={t('chat.review.edit')}
            className="review-textarea"
            onChange={(e) => setPartText(key, e.target.value)}
            rows={Math.min(opts.rows ?? 6, 14)}
            value={text}
          />
        ) : (
          <p className="review-text">
            {segments(text, spans).map((seg, i) =>
              seg.span ? (
                <button
                  aria-label={t('chat.review.markAria', { kind: kindLabel(seg.span.kind) })}
                  className={`review-mark kind-${seg.span.kind}${
                    active && active.part === key && active.start === seg.span.start
                      ? ' is-active'
                      : ''
                  }`}
                  key={i}
                  onClick={() => {
                    setActive({ part: key, start: seg.span!.start })
                    setReplacement(`[${kindLabel(seg.span!.kind)}]`)
                  }}
                  type="button"
                >
                  {seg.text}
                </button>
              ) : (
                <span key={i}>{seg.text}</span>
              ),
            )}
            {text.length === 0 && <em>{t('chat.review.emptyPart')}</em>}
          </p>
        )}
        {active && active.part === key && !isEditing && (
          <MarkActions
            kind={kindLabel(spans.find((s) => s.start === active.start)?.kind ?? 'phone')}
            onDelete={() => {
              const span = spans.find((s) => s.start === active.start)
              if (span) applyToSpan(key, span, '')
            }}
            onReplace={() => {
              const span = spans.find((s) => s.start === active.start)
              if (span) applyToSpan(key, span, replacement)
            }}
            onKeep={() => setActive(null)}
            replacement={replacement}
            setReplacement={setReplacement}
          />
        )}
        <button
          className="link-button review-edit-toggle"
          onClick={() =>
            setEditing((prev) => {
              const next = new Set(prev)
              if (next.has(key)) next.delete(key)
              else next.add(key)
              return next
            })
          }
          type="button"
        >
          {isEditing ? t('chat.review.doneEditing') : t('chat.review.edit')}
        </button>
      </div>
    )
  }

  return (
    <section aria-label={t('chat.review.title')} className="review-card">
      <header className="review-head">
        <span className="review-title">
          {flagged > 0 ? <ShieldAlert size={15} /> : <ShieldCheck size={15} />}
          <strong>{t('chat.review.title')}</strong>
        </span>
        <span className="review-dest">{destination}</span>
      </header>
      {model?.model_fallback && (
        <p className="review-summary warn" role="note">
          {t('chat.modelFallback')}
        </p>
      )}

      <p className={flagged > 0 ? 'review-summary warn' : 'review-summary'}>
        {flagged > 0
          ? t('chat.review.found', { count: flagged })
          : t('chat.review.none')}{' '}
        <small>{t('chat.review.heuristic')}</small>
      </p>
      {scanFailed && <p className="review-summary warn">{t('chat.review.scanFailed')}</p>}

      {flagged > 0 && (
        <div className="review-bulk">
          <button className="secondary-button" onClick={() => redactAll('delete')} type="button">
            {t('chat.review.deleteAll')}
          </button>
          <button
            className="secondary-button"
            onClick={() => redactAll('placeholder')}
            type="button"
          >
            {t('chat.review.maskAll')}
          </button>
        </div>
      )}

      <div className="review-part">
        <span className="review-part-title">{t('chat.review.question')}</span>
        {renderPart('q', draft.question, { rows: 3 })}
      </div>

      {draft.history.length > 0 && (
        <details className="review-part">
          <summary className="review-part-title">
            {t('chat.review.history', { count: draft.history.length })}
          </summary>
          {draft.history.map((text, i) => (
            <div className="review-history-turn" key={i}>
              <span className="review-role">
                {review.history[i]?.role === 'assistant'
                  ? t('chat.review.roleAssistant')
                  : t('chat.review.roleUser')}
              </span>
              {renderPart(`h${i}` as PartKey, text, { rows: 4 })}
            </div>
          ))}
        </details>
      )}

      {draft.refs.length > 0 && (
        <div className="review-part">
          <span className="review-part-title">
            {t('chat.review.references', { count: draft.refs.length })}
          </span>
          {draft.refs.map((ref, i) => (
            <div className={ref.include ? 'review-ref' : 'review-ref is-dropped'} key={ref.index}>
              <label className="review-ref-head">
                <input
                  checked={ref.include}
                  onChange={(e) =>
                    setDraft((d) => ({
                      ...d,
                      refs: d.refs.map((r, n) =>
                        n === i ? { ...r, include: e.target.checked } : r,
                      ),
                    }))
                  }
                  type="checkbox"
                />
                <span>
                  [{ref.index}] {review.references[i]?.document_name}
                </span>
                {!ref.include && <em>{t('chat.review.notSent')}</em>}
              </label>
              {ref.include && renderPart(`r${i}` as PartKey, ref.text, { rows: 8 })}
            </div>
          ))}
          {noneIncluded && <p className="review-summary warn">{t('chat.review.noneIncluded')}</p>}
        </div>
      )}

      <div className="review-raw">
        <button className="link-button" onClick={() => setShowRaw((v) => !v)} type="button">
          {showRaw ? t('chat.review.hideRaw') : t('chat.review.showRaw')}
        </button>
        {showRaw && (
          <pre className="review-raw-body">
            {scan.messages
              ? scan.messages.map((m) => `【${m.role}】\n${m.content}`).join('\n\n')
              : t('chat.review.noModelCall')}
          </pre>
        )}
      </div>

      {error && <div className="dialog-error">{error}</div>}

      <footer className="review-foot">
        <small>
          {changed
            ? t('chat.review.edited', { count: scan.edited_parts + scan.excluded_references })
            : t('chat.review.unchanged')}
        </small>
        <div className="review-foot-actions">
          <button className="secondary-button" disabled={busy} onClick={onCancel} type="button">
            <X size={13} /> {t('chat.review.cancel')}
          </button>
          <button
            className="primary-button"
            disabled={!canSend}
            onClick={() => onApprove(toBody(draft))}
            type="button"
          >
            {busy ? <Loader2 size={13} className="spin" /> : <Send size={13} />}{' '}
            {t('chat.review.send')}
          </button>
        </div>
      </footer>
    </section>
  )
}

function MarkActions({
  kind,
  replacement,
  setReplacement,
  onDelete,
  onReplace,
  onKeep,
}: {
  kind: string
  replacement: string
  setReplacement: (v: string) => void
  onDelete: () => void
  onReplace: () => void
  onKeep: () => void
}) {
  const { t } = useTranslation()
  return (
    <div className="review-mark-actions" role="group">
      <span>{kind}</span>
      <button className="secondary-button" onClick={onDelete} type="button">
        {t('chat.review.delete')}
      </button>
      <input
        aria-label={t('chat.review.replaceWith')}
        onChange={(e) => setReplacement(e.target.value)}
        value={replacement}
      />
      <button className="secondary-button" onClick={onReplace} type="button">
        {t('chat.review.replace')}
      </button>
      <button className="link-button" onClick={onKeep} type="button">
        {t('chat.review.keep')}
      </button>
    </div>
  )
}
