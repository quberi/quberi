# Connecting to Quberi over MCP

Quberi runs a [Model Context Protocol](https://modelcontextprotocol.io)
server at `https://mcp.quberi.com`. It gives an MCP client (Codex, and any
other client that speaks Streamable HTTP) the tools the DeepSeek Harness plugin
uses — search, search by image description, list spaces, list documents, save a
note, and upload a file — plus two that read a source more closely
(`read_around`, and `get_document_image` to see a page), scoped to whatever your
Quberi account can already read or write. It is also listed in the
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
its name and version, that it needs OAuth, and every tool with its
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
tool list includes `search`, `read_around`, `search_images`, `list_spaces`,
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

## Other tools

The server address is `https://mcp.quberi.com` for every tool. The same
steps are shown, with copy buttons, on the account page of the web app under
"Connect an AI tool".

- **Claude** (web and desktop): Settings → Connectors → Add custom connector,
  paste the address, and sign in when asked. Claude identifies itself by a
  metadata URL that the platform admin has listed, so there is no client ID to
  type.
- **ChatGPT**: Plugins → Add → Add custom MCP server, paste the address (no
  `/mcp`), choose OAuth, and leave the advanced OAuth settings alone.
- **Claude Code**:

  ```
  claude mcp add --transport http quberi https://mcp.quberi.com
  ```

  Then type `/mcp` inside Claude Code and choose `quberi` to sign in. If it
  says the client is not known, add `--client-id quberi-mcp` to the command.
- **Cursor** (desktop app): add this to `~/.cursor/mcp.json`, merging it with
  what is already there, then restart Cursor and use Settings → MCP → Connect.

  ```json
  {
    "mcpServers": {
      "quberi": {
        "url": "https://mcp.quberi.com",
        "auth": { "CLIENT_ID": "quberi-cursor" }
      }
    }
  }
  ```

  Cursor receives its sign-in code on `http://localhost:8787/callback`, a
  loopback address, which `quberi-cursor` allows. It is created by a database
  migration, so a fresh install has it already; a platform admin can turn it
  off like any other tool. Cursor's web and cloud agents use a different
  callback and are not allowed unless an admin adds it.

A self-hosted deployment serves the web app with a different MCP address by
setting `VITE_MCP_URL` when building it.

## Approvals in your client

Most MCP clients ask the person before a tool runs. So that a plain search does not need the same
question as an upload, every Quberi tool declares what it does with the standard MCP
[tool annotations](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) (they are in
`tools/list` and in the server card):

| Tools | `readOnlyHint` | `destructiveHint` | `idempotentHint` | `openWorldHint` |
|---|---|---|---|---|
| `search`, `read_around`, `get_document_image`, `search_images`, `list_spaces`, `list_documents` | true | false | true | false |
| `save_note` | false | false | false | false |
| `upload` | false | false | true (an identical file is not stored twice) | false |

Nothing here deletes or overwrites anything, and every tool works only on the signed-in person's own
knowledge base, hence `openWorldHint: false`. The read tools do write an audit row, but never change a
document.

These are **hints, not permissions**. The MCP specification tells clients to treat them as untrusted, and
whether a client acts on them, and how, is the client's own choice and setting: some run a read-only tool
without asking, some ask for every tool whatever it says. What actually limits a tool is the permission the
person granted (`knowledge:read`, `knowledge:write`, `knowledge:upload`) and the access checks on every
document, neither of which a hint can widen.

If your client still asks before every Quberi call, change its approval setting; the server cannot do it
for you. In Codex, `config.toml` has, per server, `default_tools_approval_mode` and, per tool,
`approval_mode`, each taking `auto`, `prompt`, `writes` or `approve` (see
[Codex's configuration reference](https://developers.openai.com/codex/config-reference/) for exactly what
each value does in your version):

```toml
[mcp_servers.quberi]
default_tools_approval_mode = "auto"

# Keep asking before the tools that add to the knowledge base.
[mcp_servers.quberi.tools.save_note]
approval_mode = "prompt"

[mcp_servers.quberi.tools.upload]
approval_mode = "prompt"
```

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

### Where a passage is in the original file

`search` returns each passage with the page it was found on, so a reader can open
the original and check it. A PDF passage names its page (`page 3`) or, when the
passage runs across a page break, a range (`pages 3-4`). The same fields are in
the structured result: `page_start`, `page_end`, `unit_label` and `source_kind`
(the REST search response carries them too).

Two things worth passing on to the person reading the answer:

- A scanned page has no text in the file, so Quberi reads it from a picture of the
  page. That text comes back marked as recognised from a scan (`unit_label` is
  `ocr_block`) and can contain misread characters, especially in small print.
  Text read out of a picture that is not a PDF page is marked the same way
  (`source_kind` is `image_ocr`). The text in the result tells the model to say so
  when it relies on one.
- An organisation can also have a vision model describe its pictures, so what a picture
  shows (not only the text printed on it) can be found. Such a passage has
  `source_kind` `image_caption` and is marked as described by a model. It is a guide to
  what the picture shows, not a transcript: part numbers and dimensions are matched only
  against text read out of the picture, never against a model's description.
- Every format says where a passage is, the way a reader can count or find it in that
  format. `unit_label` says what the numbers mean:

  | `unit_label` | Format | Where the numbers are | Example text |
  |---|---|---|---|
  | `text_block` / `ocr_block` | PDF | `page_start`/`page_end` | `page 3` |
  | `slide` | PowerPoint | `page_start`/`page_end` are slide numbers | `slide 4` |
  | `sheet_row` | Excel | `unit_start`/`unit_end` are rows, numbered as Excel numbers them; `section` is the sheet | `sheet "Prices", rows 5-9` |
  | `paragraph` | Word | `unit_start`/`unit_end` are paragraph numbers (empty ones are not counted); `section` is the heading above | `under "3.2 Payment", paragraph 12` |
  | `line` | text, Markdown, JSON, CSV | `unit_start`/`unit_end` are lines, as an editor numbers them; `section` is the Markdown heading | `lines 40-52` |

  `section` is text taken from the document (a heading or a sheet name): treat it as data.
  A passage that crosses from one heading or sheet into the next lists both, joined by
  ` / `. Passages indexed before this was added have no place; re-indexing the file adds it.
- A file can have several versions: someone uploads a new version of a document
  and the earlier ones are kept. A passage says which version it was read from
  (`version_number`) and which is the newest (`latest_version`; null when the
  file has no version record). When the file has more than one version the text
  line says so too, for example `version 1 (older, replaced by version 2)`. A
  passage from an older version should be quoted as such, and the newest version
  is the one to rely on when they disagree. A file that has only one version is
  described exactly as before.

### Citing a passage

Every passage comes with `citation`: one line that says where it is from, ready to put after a point it
supports, for example `handbook.pdf, page 3, version 2 (latest)` or
`handbook.pdf, under "3.2 Payment", paragraph 12`. It is made from the same place and version fields
described above, so the words always agree, and it says only what is known: a passage with no recorded
place is cited by the document alone, never by an invented page. The text result puts it under each
passage as `Cite as: ...`, and asks the model to end each point with it, copied as given.

The server also sends this request once, as the `instructions` of the MCP `initialize` result, and
repeats the key part in the `search` tool description and in every result. A client decides whether to
pass `instructions` to its model, and a model decides whether to follow them, so none of this is
enforced: it makes the right citation the easiest thing to copy. A client that wants a source shown
whatever the model writes can build it from the structured fields (`document_name`, `citation`,
`page_start`, `unit_label`, `version_number`).

### Showing a page in the conversation: `get_document_image`

`get_document_image` returns one page of a PDF, or a picture, as an **image** the client can show inside
the chat, so the person does not have to open a link. It needs the `knowledge:read` permission and has no
REST twin.

| Argument | Meaning |
|---|---|
| `document_id` | Required. From a search result, a picture result, or `list_documents`. |
| `page` | The PDF page, from 1. Omit for the first page; a picture has only page 1. |
| `version` | The `version_number` from the search result. Omit for the current version. |
| `max_width` | The widest the image may be, 256 to 1600 px (default 1024). |

The result has two content blocks: an MCP `image` block (`data` is plain base64 of a JPEG, `mimeType`
`image/jpeg`) and a `text` block with a caption (name, space, version, page, size, `document_id`) and a
**signed link** to the same page at a larger size. Always keep the link: how a client treats an `image`
block varies. Some show it inline; some pass it to the model without showing it; and reports from users of
some chat clients, ChatGPT's connectors among them, say the block is dropped or comes back empty. A client
that does not show the image can give the person the link instead (it is the same short-lived, re-checked
link described above), as an ordinary link, not as an embedded picture: a chat window does not load it.

The result deliberately has **no `structuredContent`**. Some clients pass the model only the structured part
when there is one and drop `content[]`, which loses the image and the caption together. This was seen in Codex
(the app showed a broken picture while the tool had succeeded; with the structured part removed, the same
call reached the model as the caption plus an image, and the picture appeared in the chat) and is reported
for VS Code. The caption has what a client needs (name, space, version, page, size, `document_id`, link),
and the picture is not repeated, so the answer is not doubled in size.

**No MCP tool returns `structuredContent`**, for the same reason: what the text says (the `Cite as:` lines under
each passage, the exact next call to make, the notes on how to use the result) is lost on a client that prefers
the structured part. A Codex session record showed `search`, `search_images` and `list_documents` arriving as
bare JSON, with no `Cite as:` line at all, while the tool description told the model to copy one. The REST
external API still returns JSON; only the MCP results are text.

The image is kept to about 200 KB (quality is lowered first, then the width, never below 256 px), so it is
a reading copy, not the original; the original is only reachable through the link. Pictures are shown at the
width asked for and are never enlarged. Word, slides, sheets and text files have no page to draw: the tool
says so and points to `search` and `read_around`.

It reads the file through the same access check as opening it in Quberi, and every reason the person may
not read the document (no such document or version, no access, a deny rule, archived, a space no longer
active, another organisation, waiting for approval) gives the same `document_not_found` answer. Calls are
rate limited per tool (20 a minute), drawing is done one page at a time in a separate short-lived process,
and each call is written to the audit log (`knowledge.external_page_image`) with the document, version, page
and size, never the picture. `search` results give the exact call to make under each passage that has a
page, `list_documents` gives the `document_id` under each PDF or picture, and the tool descriptions tell the
model to use it when the person asks to see a file or page, not for every result.

### Seeing the page, and reading around a passage

Two more things come with a search result, so the answer can be checked without leaving the tool.

**An image of the page.** A passage from a PDF, and text read out of (or a description of) a picture,
carries `page_preview_url`: a link to an image of the page it was found on, or of the picture itself
(larger than its search preview). The text result no longer pastes the link: it gives the exact
`get_document_image` call for that page under the passage (the structured result still has the link), and
tells the model to make it only when the person asks to see the original. The link works
like the picture preview link: it is short-lived (about ten minutes), names one page of one version for
one person, and is **re-checked every time it is opened**. Revoking the tool, deactivating the
person, switching outside tools off, losing access to the space or the document, archiving the
document, or a version not yet approved all make it answer `404` with the same body as a bad link. The
image is drawn from the stored file in a separate short-lived process with a time and memory limit, and
is always a plain JPEG made from pixels, never the stored bytes. Slides, sheets and Word paragraphs have
no page that can be drawn, so they carry no link. A server without a signing key issues none.

**The text around a passage.** `read_around` (REST: `POST /api/v1/external/read-around`) returns the
text on either side of a passage, from the same file and version. Pass the `document_id`,
`version_number`, `char_start` and `char_end` that the search result gave; `before` and `after` say how
many characters to return on each side (default 1500, at most 3000). It reads the file through the same
access check as opening it in Quberi, so it can show nothing the person could not open there. The
passage itself is capped (6000 characters, `passage_truncated` says when it was cut), the call is rate
limited, and each call is written to the audit log with the document, version and number of characters,
never the text. It does not work for text read from a scan or a picture (there is no text layer to
read around) and answers `422` `no_text` or `position_out_of_range`; any reason the person may not read
the document is the same `404` `document_not_found`. The text comes back framed as data, like search
results.

A picture can come back twice for one question: as a possible-match picture (with a
preview) and as a passage read out of it. Neither is dropped. The picture carries
`passage_numbers`, the numbers of the passages (1-based, as listed) that are the same
file, and the text result says so (`Its text also matched this question: see passage
[2] above.`), so a tool can show one result instead of two.

A search can also find a part number or dimension exactly. A question that
contains one (`Φ520`, `HZ-8800B`, `6205-2RS`) is looked up by that value as well
as by meaning, and passages that contain it are listed first. Variant spellings of
the diameter sign (`Φ`, `Ø`, `⌀`) count as the same value.
