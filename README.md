# Quberi

Quberi is a multi-tenant AI knowledge base for teams. You upload documents,
members ask questions in chat, and answers come from retrieved passages.
Retrieval is filtered by what the asking user is allowed to see.

**Permissions are per-document, not per-space.** Within one space, two users can
see different documents. A deny rule on a document wins over any allow rule or
space role, retrieval is filtered inside the vector index, and rules are
evaluated on every request, so a change applies to the next one.

Licensed under [AGPL-3.0](LICENSE).

> Status: early. Expect rough edges. There are no prebuilt images or releases
> yet, and the stack is built from source.

## What it does

- **Permission-aware retrieval, per document.** Documents live in spaces, and a
  space grants access by organization, unit, position, group or individual
  member, each with a role. On top of that, any single document can be shared
  with someone who cannot read its space, or hidden from someone who can.
  Every question is checked against both.
- **Audit log.** Sensitive actions are recorded, including searches made by
  connected outside tools.
- **Send review.** Before a question and the retrieved passages go to the chat
  model, you can preview them, edit them, or drop individual passages.
- **Models.** Bring your own key, use an organization-level model, or use a
  platform default. Chat uses OpenAI-compatible endpoints. Embeddings (BGE-M3)
  run inside the API and worker processes on CPU, not through a third-party API.
- **Agents.** Build knowledge assistants and connect external agents (Coze, Dify).
- **Sign-in and sync.** Email code, Google or Feishu login. Feishu documents can
  be synced in.
- **Connected tools.** An organization admin can allow read-only search from
  outside tools through OAuth (authorization code with PKCE). It is off by
  default. A plugin for DeepSeek Harness is published separately as
  [`dsh-quberi`](https://github.com/quberi/dsh-quberi). An
  [MCP server](https://mcp.quberi.com) (listed in the
  [official MCP Registry](https://registry.modelcontextprotocol.io) as
  `com.quberi/mcp`) exposes the same six tools to Codex and other MCP
  clients — see [`docs/mcp-server.md`](docs/mcp-server.md) for how to
  connect.
- **Member offboarding.** The organization owner can deactivate a departing
  member: it ends their web sessions, outside-tool access and Feishu sync,
  removes their private model keys, and freezes their personal space. See
  [`docs/member-offboarding.md`](docs/member-offboarding.md).

## Run it locally

Requirements: Docker with Compose, and `pnpm` if you want to work on the web app.

```bash
cp .env.example .env     # development values; change every secret before sharing
# Generate the required encryption key and put it in .env as QUBERI_ENCRYPTION_KEY:
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
make up                  # builds and starts the full stack
```

`QUBERI_ENCRYPTION_KEY` encrypts stored API keys and tokens. Keep it: if you lose
it, stored credentials cannot be read again.

Then open <http://localhost:8080>. The API listens on <http://localhost:8000>.

The stack is Postgres, Redis, Qdrant, MinIO (via the community fork
`pgsty/silo`), the API, a background worker and the web app. See
[`deploy/README.md`](deploy/README.md) for details (it is written in Chinese).

### Signing in locally

Real sign-in needs SMTP, Google or Feishu to be configured in `.env`.
In a non-production environment (`QUBERI_ENVIRONMENT=development`, the default in
`.env.example`), email verification codes are also written to the API log, so you
can try the app without a mail server:

```bash
docker compose --env-file .env --profile app logs api | grep "DEV verification code"
```

Do not run with `QUBERI_ENVIRONMENT=development` on a machine that others can reach.

### Platform admin console

The operator's console lives at <http://localhost:8080/admin>. It is separate from
member sign-in and is for looking after organizations, platform models and usage.

On first boot the API creates one platform admin account (`admin`) with a random
password and writes it **once** to the API log:

```bash
docker compose --env-file .env --profile app logs api | grep "PLATFORM ADMIN CREATED"
```

Sign in with it; the console asks you to choose your own password (12 to 128
characters) before anything else. There is no built-in default password. To pick
the first password yourself, set `QUBERI_BOOTSTRAP_ADMIN_PASSWORD` in `.env`
before the first start; a weak value is ignored and a random one is generated.

Because the generated password is printed to the container log, change it right
away, and treat old logs as sensitive until you have.

`QUBERI_PLATFORM_ADMIN_TOKEN` still works as a bearer token for scripts and `curl`
against `/api/v1/admin/*`, but the console itself only accepts a username and
password. Leave the token empty if you do not need it. Sign-in is rate limited:
5 failed attempts on an account lock it until 15 minutes after the first of those
failures, even for the right password.

The console is available in English and Simplified Chinese. Switch language at the
bottom of the sidebar (or below the sign-in card); it follows your browser language
the first time and remembers your choice. Error messages from the API follow the same
language. See [apps/web/src/admin/i18n/README.md](apps/web/src/admin/i18n/README.md)
for how to add a string, a language or a message code.

### First run and resources

- The first question or upload loads the BGE-M3 embedding model, which is
  downloaded from Hugging Face into a Docker volume (about 4 GB on disk). If
  Hugging Face is not reachable from your network, the download will fail.
- In one measurement, loading BGE-M3 on CPU took about 2.4 GB of memory at peak.
  The API and worker each load their own copy. We have not measured the whole
  stack under load, so treat 16 GB of RAM as a comfortable starting point rather
  than a tested requirement.

## What stays on your machine

When you run the stack yourself, uploaded documents (MinIO), vectors (Qdrant),
conversations and the audit log (Postgres) are stored on the machines you run.
Embeddings are computed locally once the model is cached.

This is not an air-gapped setup. Questions and the retrieved passages are sent to
whichever chat model you configure. If that is a cloud API, they leave your
network. Login codes go through your SMTP provider, Google or Feishu sign-in
talks to those services, and the first model download comes from Hugging Face.
We have not tested the app against a fully local chat model.

## Development

```bash
make check     # lint, migration drift check, API and web tests
make test-api  # needs a separate quberi_test database, see deploy/README.md
make test-web
```

Layout:

- `apps/api` — FastAPI service, Alembic migrations, RAG worker
- `apps/web` — React app
- `compose.yaml` — local stack; `deploy/` — production overlay (Caddy) and notes

## Security

Please do not open a public issue for a vulnerability. Use GitHub's
**Security → Report a vulnerability** to report it privately. If that is not
possible, email [chaney1720@gmail.com](mailto:chaney1720@gmail.com). No response
time is promised yet.

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
Contributions are accepted under the [Developer Certificate of Origin](DCO): sign
off each commit with `git commit -s`. There is no CLA, and your contribution is
licensed under AGPL-3.0 like the rest of the project.

## License

Quberi is licensed under the GNU Affero General Public License v3.0 only. If you
modify it and offer it to others over a network, you must make your modified
source available to those users. See [LICENSE](LICENSE).
