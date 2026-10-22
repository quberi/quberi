# Connecting to Quberi over MCP

Quberi runs a [Model Context Protocol](https://modelcontextprotocol.io)
server at `https://mcp.quberi.com`. It gives an MCP client (Codex, and any
other client that speaks Streamable HTTP) the same six tools the DeepSeek
Harness plugin uses — search, search by image description, list spaces, list
documents, save a note, and upload a file — scoped to whatever your Quberi
account can already read or write. It is also listed in the
[official MCP Registry](https://registry.modelcontextprotocol.io) as
`com.quberi/mcp`.

## Before you connect

You need a Quberi account whose organization has outside tools turned on
(an organization owner or admin can enable this from the connected-tools
page). The first connection opens a normal sign-in-and-consent page in your
browser — nothing about this requires a separate account or an API key.

## Server card

For directories that list MCP servers but cannot sign in to scan this one,
the server publishes a static description of itself at
`https://mcp.quberi.com/.well-known/mcp/server-card.json`
([SEP-1649](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1649)):
its name and version, that it needs OAuth, and all six tools with their
input schemas and the permission each needs. It is generated from the same
tool table the server uses, needs no sign-in, and says nothing about any
person or organisation.

## Configuring the MCP client

Quberi's authorization server does not implement open automatic client
registration: there is no
[Dynamic Client Registration](https://www.rfc-editor.org/rfc/rfc7591), and it
never fetches an arbitrary tool's
[Client ID Metadata Document](https://modelcontextprotocol.io/specification/draft/basic/authorization/client-registration).
This is deliberate: which tools can start a login is a short list that a
platform admin maintains (the "Outside tools" page of the platform admin
console, stored in the `oauth_clients` table), not something a client can
grant itself by showing up. Each entry says which client ID the tool presents
and the exact callback addresses it may be sent its code to. A tool that is
not on the list fails before it ever shows you a sign-in page.
Two kinds of entry exist. A **registered** client ID (`quberi-mcp`) is passed
explicitly by the tool's user, as described below. A tool that identifies
itself by the URL of its own metadata document (ChatGPT, Claude Code) can be
listed by that URL, and the server then advertises
`client_id_metadata_document_supported` so those tools connect with no ID to
type. What is trusted about such a tool is only what the admin wrote down:
nothing is fetched from its URL.
What this means in practice: **pass the pre-registered client ID
explicitly** when you add the server. For Codex CLI:

```
codex mcp add quberi --url https://mcp.quberi.com --oauth-client-id quberi-mcp
codex mcp login quberi
```

`codex mcp login` opens your browser to Quberi's sign-in and consent page.
Approve the connection there (choosing which organization's knowledge it can
reach), and the CLI stores the resulting token itself. From then on, Codex's
tool list includes `search`, `search_images`, `list_spaces`,
`list_documents`, `save_note`, and `upload` — filtered to what your account's
scope allows.

If you add the server **without** `--oauth-client-id`, Codex defaults to
trying automatic registration first. That fails with an error naming
"Dynamic client registration" or "Dynamic Client Registration not
supported" — at that point, remove the server entry and re-add it with the
flag above rather than retrying:

```
codex mcp remove quberi
codex mcp add quberi --url https://mcp.quberi.com --oauth-client-id quberi-mcp
codex mcp login quberi
```

Other MCP clients that support pre-registered credentials should have an
equivalent option (sometimes called a static or manual client ID) — look for
a way to supply `quberi-mcp` as the client ID before the client's login flow
starts, rather than letting it attempt registration.

## What a connected client can do

A connection acts as the person who approved it, in the organization they
chose — exactly the same permission boundary as signing in to the Quberi web
app. It cannot see or change anything that account could not already see or
change there. Revoking access works the same way it does for any other
outside tool: from the connected-tools page, or automatically if the member
is deactivated (see [member offboarding](./member-offboarding.md)).

### Uploading a file that is already there

A space keeps one copy of a file. If `upload` is given a file with exactly the
same content (the same SHA-256) as one already in that space, nothing is stored:
the tool answers that the file is already there and names it, so the client
does not need to guess from file names. The same rule applies to uploads in the
web app, which refuses the second copy. A file waiting for approval counts; one
that was rejected does not. If a rule hides the existing file from you, the
answer says an identical file exists without naming it. The check is per space
and exact: the same picture saved again at a different size or quality is a
different file.
