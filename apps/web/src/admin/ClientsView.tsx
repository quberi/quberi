import { Pencil, Plug, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { useEffect, useState } from 'react'

import {
  createOAuthClient,
  deleteOAuthClient,
  listOAuthClients,
  updateOAuthClient,
  type OAuthClientInput,
  type OAuthClientKind,
  type OAuthClientList,
  type OAuthClientRow,
  type RejectedAttempt,
} from './api'
import { useLang } from './i18n'
import { errorMessage } from './i18n/errors'
import { T } from './i18n/Rich'

const KNOWN_REASONS = ['unknown_client', 'redirect_not_allowed'] as const

/** What the editor starts from: a tool being edited, or a blank form (optionally prefilled). */
type Draft = { id: string | null } & OAuthClientInput

const BLANK: OAuthClientInput = {
  client_id: '',
  kind: 'metadata_url',
  name: '',
  redirect_uris: [],
  enabled: true,
  allow_mcp: true,
  note: null,
}

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]'])

/**
 * A starting point for allowing a tool that was just refused, from what it really presented.
 *
 * - A callback on the person's own machine becomes the word ``loopback`` (its port changes on
 *   every run, so the address itself can never be what is allowed).
 * - Tools often put one opaque id in both their client id and their callback (ChatGPT's
 *   ``.../oauth/<id>/client.json`` and ``.../connector/oauth/<id>``; Codex's loopback
 *   ``/callback/<id>``). When the callback ends in a segment that also appears as a whole
 *   segment of the client id, that segment is turned into ``{id}`` so the entry covers every
 *   connection of the tool, not just this one.
 *
 * It is only a draft: the admin reads it before saving.
 */
export function draftFromAttempt(a: RejectedAttempt): Draft {
  let clientId = a.client_id
  let redirect = ''
  try {
    const target = new URL(a.redirect_uri)
    const lastSegment = target.pathname.split('/').filter(Boolean).pop() ?? ''
    if (target.protocol === 'http:' && LOOPBACK_HOSTS.has(target.hostname)) redirect = 'loopback'
    else if (target.protocol === 'https:') redirect = a.redirect_uri
    if (a.client_id.startsWith('https://') && /^[A-Za-z0-9_-]{4,64}$/.test(lastSegment)) {
      const url = new URL(a.client_id)
      const segments = url.pathname.split('/')
      if (segments.includes(lastSegment)) {
        const swapped = segments.map((s) => (s === lastSegment ? '{id}' : s)).join('/')
        clientId = `${url.origin}${swapped}`
        if (redirect === a.redirect_uri) {
          redirect = redirect.replace(`/${lastSegment}`, '/{id}')
        }
      }
    }
  } catch {
    // A callback we cannot parse is left for the admin to type.
  }
  return {
    id: null,
    ...BLANK,
    kind: a.client_id.startsWith('https://') ? 'metadata_url' : 'registered',
    client_id: clientId,
    redirect_uris: redirect ? [redirect] : [],
  }
}

/**
 * The outside tools (ChatGPT, Claude, Codex, ...) that may ask a person for access.
 *
 * This decides where an authorisation code may be delivered, so it is deliberately plain:
 * each tool has the id it presents and the exact callback addresses it may be sent to. Nothing
 * is looked up from the tool's own URL. Switching a tool off stops new logins and its refresh
 * tokens at once.
 */
export function ClientsView({ token }: { token: string }) {
  const { t, tCode, formatDateTime } = useLang()
  const [data, setData] = useState<OAuthClientList | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<{ err: unknown } | null>(null)
  const [editing, setEditing] = useState<Draft | null>(null)

  const load = async (isRefresh: boolean) => {
    isRefresh ? setRefreshing(true) : setLoading(true)
    setError(null)
    try {
      setData(await listOAuthClients(token))
    } catch (err) {
      setError({ err })
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => {
    void load(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggle = async (row: OAuthClientRow) => {
    try {
      await updateOAuthClient(token, row.id, { enabled: !row.enabled })
      void load(true)
    } catch (err) {
      window.alert(errorMessage(err, tCode, t))
    }
  }

  const remove = async (row: OAuthClientRow) => {
    if (
      !window.confirm(t('clients.confirmRemove', { name: row.name }))
    ) {
      return
    }
    try {
      await deleteOAuthClient(token, row.id)
      void load(true)
    } catch (err) {
      window.alert(errorMessage(err, tCode, t))
    }
  }

  return (
    <section className="admin-section">
      <div className="admin-section-head">
        <h2>
          <Plug size={16} /> {t('clients.title')}
        </h2>
        <div className="admin-section-actions">
          <button disabled={refreshing} onClick={() => void load(true)} type="button">
            <RefreshCw size={14} className={refreshing ? 'spin' : undefined} />{' '}
            {t('common.refresh')}
          </button>
          <button
            className="admin-button-primary"
            onClick={() => setEditing({ id: null, ...BLANK })}
            type="button"
          >
            <Plus size={14} /> {t('clients.addTool')}
          </button>
        </div>
      </div>
      <p className="admin-section-hint">
        <T k="clients.hint" />
      </p>

      {loading && <div className="admin-state">{t('common.loading')}</div>}
      {error && !loading && (
        <div className="admin-state admin-state-error">{errorMessage(error.err, tCode, t)}</div>
      )}

      {!loading && !error && data && (
        <>
          <p className="admin-section-hint" data-testid="advertising">
            <T
              k={
                data.advertises_metadata_documents
                  ? 'clients.advertising.on'
                  : 'clients.advertising.off'
              }
            />
          </p>
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>{t('clients.col.tool')}</th>
                  <th>{t('clients.col.clientId')}</th>
                  <th>{t('clients.col.sentTo')}</th>
                  <th>{t('clients.col.status')}</th>
                  <th aria-label={t('clients.col.actions')} />
                </tr>
              </thead>
              <tbody>
                {data.items.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <strong>{row.name}</strong>
                      <small>
                        {t(`clients.kind.${row.kind}`)} ·{' '}
                        {row.allow_mcp ? t('clients.mcpServer') : t('clients.restOnly')}
                      </small>
                    </td>
                    <td>
                      <code className="admin-code-break">{row.client_id}</code>
                    </td>
                    <td>
                      {row.redirect_uris.map((r) => (
                        <div key={r}>
                          <code className="admin-code-break">{r}</code>
                        </div>
                      ))}
                    </td>
                    <td>
                      <button
                        aria-pressed={row.enabled}
                        className={`admin-chip ${row.enabled ? 'admin-chip-status-active' : 'admin-chip-status-paused'}`}
                        onClick={() => void toggle(row)}
                        title={row.enabled ? t('clients.switchOff') : t('clients.switchOn')}
                        type="button"
                      >
                        {row.enabled ? t('clients.on') : t('clients.off')}
                      </button>
                    </td>
                    <td className="admin-table-row-actions">
                      <button
                        onClick={() => setEditing({ id: row.id, ...toInput(row) })}
                        title={t('common.edit')}
                        type="button"
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        className="admin-danger"
                        onClick={() => void remove(row)}
                        title={t('common.remove')}
                        type="button"
                      >
                        <Trash2 size={13} />
                      </button>
                    </td>
                  </tr>
                ))}
                {data.items.length === 0 && (
                  <tr>
                    <td colSpan={5}>{t('clients.empty')}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <h3>{t('clients.refused.title')}</h3>
          <p className="admin-section-hint">
            {t('clients.refused.hint')}
          </p>
          {data.rejected.length === 0 ? (
            <div className="admin-state">{t('clients.refused.empty')}</div>
          ) : (
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>{t('clients.col.clientId')}</th>
                    <th>{t('clients.refused.col.callback')}</th>
                    <th>{t('clients.refused.col.why')}</th>
                    <th>{t('clients.refused.col.when')}</th>
                    <th aria-label={t('clients.col.actions')} />
                  </tr>
                </thead>
                <tbody>
                  {data.rejected.map((a) => (
                    <tr key={`${a.client_id}|${a.redirect_uri}|${a.reason}`}>
                      <td>
                        <code className="admin-code-break">{a.client_id}</code>
                      </td>
                      <td>
                        <code className="admin-code-break">{a.redirect_uri}</code>
                      </td>
                      <td>
                        {KNOWN_REASONS.find((r) => r === a.reason)
                          ? t(`clients.reason.${a.reason as (typeof KNOWN_REASONS)[number]}`)
                          : a.reason}
                      </td>
                      <td>{formatDateTime(a.at)}</td>
                      <td className="admin-table-row-actions">
                        {a.reason === 'unknown_client' && (
                          <button onClick={() => setEditing(draftFromAttempt(a))} type="button">
                            {t('clients.allow')}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {editing && (
        <ClientEditorDialog
          draft={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            void load(true)
          }}
          token={token}
        />
      )}
    </section>
  )
}

function toInput(row: OAuthClientRow): OAuthClientInput {
  return {
    client_id: row.client_id,
    kind: row.kind,
    name: row.name,
    redirect_uris: row.redirect_uris,
    enabled: row.enabled,
    allow_mcp: row.allow_mcp,
    note: row.note,
  }
}

function ClientEditorDialog({
  token,
  draft,
  onClose,
  onSaved,
}: {
  token: string
  draft: Draft
  onClose: () => void
  onSaved: () => void
}) {
  const { t, tCode } = useLang()
  const isNew = draft.id === null
  const [name, setName] = useState(draft.name)
  const [kind, setKind] = useState<OAuthClientKind>(draft.kind)
  const [clientId, setClientId] = useState(draft.client_id)
  const [redirects, setRedirects] = useState(draft.redirect_uris.join('\n'))
  const [enabled, setEnabled] = useState(draft.enabled)
  const [allowMcp, setAllowMcp] = useState(draft.allow_mcp)
  const [note, setNote] = useState(draft.note ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<{ err: unknown } | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setError(null)
    const input: OAuthClientInput = {
      client_id: clientId.trim(),
      kind,
      name: name.trim(),
      redirect_uris: redirects
        .split('\n')
        .map((r) => r.trim())
        .filter(Boolean),
      enabled,
      allow_mcp: allowMcp,
      note: note.trim() || null,
    }
    try {
      if (draft.id === null) await createOAuthClient(token, input)
      else await updateOAuthClient(token, draft.id, input)
      onSaved()
    } catch (err) {
      setError({ err })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="admin-drawer-backdrop" onClick={onClose} role="presentation">
      <aside
        aria-modal="true"
        className="admin-drawer"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
      >
        <header className="admin-drawer-head">
          <strong>{isNew ? t('clients.editor.addTitle') : t('clients.editor.editTitle', { name: draft.name })}</strong>
          <button aria-label={t('common.close')} onClick={onClose} type="button">
            <X size={16} />
          </button>
        </header>
        <form className="admin-drawer-body admin-form" onSubmit={submit}>
          <label className="admin-field">
            <span>{t('clients.editor.name')}</span>
            <input onChange={(e) => setName(e.target.value)} required value={name} />
          </label>
          <label className="admin-field">
            <span>{t('clients.editor.kind')}</span>
            <select onChange={(e) => setKind(e.target.value as OAuthClientKind)} value={kind}>
              <option value="metadata_url">{t('clients.kind.metadata_url')}</option>
              <option value="registered">{t('clients.kind.registered')}</option>
            </select>
          </label>
          <label className="admin-field">
            <span>{t('clients.col.clientId')}</span>
            <input
              onChange={(e) => setClientId(e.target.value)}
              placeholder={
                kind === 'metadata_url' ? 'https://example.com/oauth/{id}/client.json' : 'my-tool'
              }
              required
              value={clientId}
            />
          </label>
          <label className="admin-field">
            <span>{t('clients.editor.sentTo')}</span>
            <textarea
              onChange={(e) => setRedirects(e.target.value)}
              placeholder={'loopback\nhttps://example.com/callback'}
              required
              rows={4}
              value={redirects}
            />
          </label>
          <label className="admin-checkbox-option">
            <input checked={enabled} onChange={(e) => setEnabled(e.target.checked)} type="checkbox" />
            <span>{t('clients.editor.switchedOn')}</span>
          </label>
          <label className="admin-checkbox-option">
            <input checked={allowMcp} onChange={(e) => setAllowMcp(e.target.checked)} type="checkbox" />
            <span>
              {t('clients.editor.allowMcp')}
              <small>{t('clients.editor.allowMcpHint')}</small>
            </span>
          </label>
          <label className="admin-field">
            <span>{t('clients.editor.note')}</span>
            <input onChange={(e) => setNote(e.target.value)} value={note} />
          </label>
          {error && (
            <div className="admin-state admin-state-error">
              {errorMessage(error.err, tCode, t)}
            </div>
          )}
          <div className="admin-form-actions">
            <button disabled={saving} onClick={onClose} type="button">
              {t('common.cancel')}
            </button>
            <button className="admin-button-primary" disabled={saving} type="submit">
              {saving ? t('common.saving') : t('common.save')}
            </button>
          </div>
        </form>
      </aside>
    </div>
  )
}
