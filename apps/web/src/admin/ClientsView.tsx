import { Pencil, Plug, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { useEffect, useState } from 'react'

import {
  AdminApiError,
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

const KIND_LABEL: Record<OAuthClientKind, string> = {
  registered: 'Registered id',
  metadata_url: 'Metadata document URL',
}

const REASON_LABEL: Record<string, string> = {
  unknown_client: 'Unknown tool',
  redirect_not_allowed: 'Callback address not allowed',
}

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

function draftFromAttempt(a: RejectedAttempt): Draft {
  return {
    id: null,
    ...BLANK,
    kind: a.client_id.startsWith('https://') ? 'metadata_url' : 'registered',
    client_id: a.client_id,
    // Only a starting point: the admin decides what to trust.
    redirect_uris: a.redirect_uri.startsWith('https://') ? [a.redirect_uri] : [],
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
  const [data, setData] = useState<OAuthClientList | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<Draft | null>(null)

  const load = async (isRefresh: boolean) => {
    isRefresh ? setRefreshing(true) : setLoading(true)
    setError(null)
    try {
      setData(await listOAuthClients(token))
    } catch (err) {
      setError(err instanceof AdminApiError ? err.message : String(err))
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
      window.alert(err instanceof AdminApiError ? err.message : String(err))
    }
  }

  const remove = async (row: OAuthClientRow) => {
    if (
      !window.confirm(
        `Remove "${row.name}"? People can no longer connect it, and its existing connections stop refreshing.`,
      )
    ) {
      return
    }
    try {
      await deleteOAuthClient(token, row.id)
      void load(true)
    } catch (err) {
      window.alert(err instanceof AdminApiError ? err.message : String(err))
    }
  }

  return (
    <section className="admin-section">
      <div className="admin-section-head">
        <h2>
          <Plug size={16} /> Outside tools
        </h2>
        <div className="admin-section-actions">
          <button disabled={refreshing} onClick={() => void load(true)} type="button">
            <RefreshCw size={14} className={refreshing ? 'spin' : undefined} /> Refresh
          </button>
          <button
            className="admin-button-primary"
            onClick={() => setEditing({ id: null, ...BLANK })}
            type="button"
          >
            <Plus size={14} /> Add tool
          </button>
        </div>
      </div>
      <p className="admin-section-hint">
        The tools that may ask a person for access to their knowledge. Each one has the id it
        presents and the exact callback addresses it may be sent its code to: <code>loopback</code>{' '}
        means a callback on the person's own machine; anything else must be an exact{' '}
        <code>https://</code> address, and <code>{'{id}'}</code> stands for one short segment that
        must be the same in the tool's id and its callback. Nothing is fetched from a tool's URL.
      </p>

      {loading && <div className="admin-state">Loading…</div>}
      {error && !loading && <div className="admin-state admin-state-error">{error}</div>}

      {!loading && !error && data && (
        <>
          <p className="admin-section-hint" data-testid="advertising">
            Client ID metadata documents:{' '}
            <strong>{data.advertises_metadata_documents ? 'advertised' : 'not advertised'}</strong>
            {data.advertises_metadata_documents
              ? '. Tools such as Claude Code identify themselves by URL, so keep their entries switched on.'
              : '. Switch on a tool that uses a metadata document URL to advertise it.'}
          </p>
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Tool</th>
                  <th>Client id</th>
                  <th>May be sent its code to</th>
                  <th>Status</th>
                  <th aria-label="actions" />
                </tr>
              </thead>
              <tbody>
                {data.items.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <strong>{row.name}</strong>
                      <small>
                        {KIND_LABEL[row.kind]} · {row.allow_mcp ? 'MCP server' : 'REST API only'}
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
                        title={row.enabled ? 'Click to switch off' : 'Click to switch on'}
                        type="button"
                      >
                        {row.enabled ? 'on' : 'off'}
                      </button>
                    </td>
                    <td className="admin-table-row-actions">
                      <button
                        onClick={() => setEditing({ id: row.id, ...toInput(row) })}
                        title="Edit"
                        type="button"
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        className="admin-danger"
                        onClick={() => void remove(row)}
                        title="Remove"
                        type="button"
                      >
                        <Trash2 size={13} />
                      </button>
                    </td>
                  </tr>
                ))}
                {data.items.length === 0 && (
                  <tr>
                    <td colSpan={5}>No tools are allowed, so nobody can connect one.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <h3>Recently refused</h3>
          <p className="admin-section-hint">
            Attempts by signed-in people that were turned away. A tool you want to allow shows
            up here with the id and callback it actually used.
          </p>
          {data.rejected.length === 0 ? (
            <div className="admin-state">Nothing has been refused lately.</div>
          ) : (
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Client id</th>
                    <th>Callback it asked for</th>
                    <th>Why</th>
                    <th>When</th>
                    <th aria-label="actions" />
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
                      <td>{REASON_LABEL[a.reason] ?? a.reason}</td>
                      <td>{new Date(a.at).toLocaleString()}</td>
                      <td className="admin-table-row-actions">
                        {a.reason === 'unknown_client' && (
                          <button onClick={() => setEditing(draftFromAttempt(a))} type="button">
                            Allow…
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
  const isNew = draft.id === null
  const [name, setName] = useState(draft.name)
  const [kind, setKind] = useState<OAuthClientKind>(draft.kind)
  const [clientId, setClientId] = useState(draft.client_id)
  const [redirects, setRedirects] = useState(draft.redirect_uris.join('\n'))
  const [enabled, setEnabled] = useState(draft.enabled)
  const [allowMcp, setAllowMcp] = useState(draft.allow_mcp)
  const [note, setNote] = useState(draft.note ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
      setError(err instanceof AdminApiError ? err.message : String(err))
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
          <strong>{isNew ? 'Allow a tool' : `Edit ${draft.name}`}</strong>
          <button aria-label="Close" onClick={onClose} type="button">
            <X size={16} />
          </button>
        </header>
        <form className="admin-drawer-body admin-form" onSubmit={submit}>
          <label className="admin-field">
            <span>Name shown to people</span>
            <input onChange={(e) => setName(e.target.value)} required value={name} />
          </label>
          <label className="admin-field">
            <span>How the tool names itself</span>
            <select onChange={(e) => setKind(e.target.value as OAuthClientKind)} value={kind}>
              <option value="metadata_url">{KIND_LABEL.metadata_url}</option>
              <option value="registered">{KIND_LABEL.registered}</option>
            </select>
          </label>
          <label className="admin-field">
            <span>Client id</span>
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
            <span>May be sent its code to (one per line)</span>
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
            <span>Switched on</span>
          </label>
          <label className="admin-checkbox-option">
            <input checked={allowMcp} onChange={(e) => setAllowMcp(e.target.checked)} type="checkbox" />
            <span>
              May use the MCP server
              <small>Leave off for a tool that only uses the REST API.</small>
            </span>
          </label>
          <label className="admin-field">
            <span>Note (optional)</span>
            <input onChange={(e) => setNote(e.target.value)} value={note} />
          </label>
          {error && <div className="admin-state admin-state-error">{error}</div>}
          <div className="admin-form-actions">
            <button disabled={saving} onClick={onClose} type="button">
              Cancel
            </button>
            <button className="admin-button-primary" disabled={saving} type="submit">
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </form>
      </aside>
    </div>
  )
}
