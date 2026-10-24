import type { Dict } from './index'

/**
 * English strings of the platform admin console. This file defines the key set:
 * `zh.ts` is typed `Record<MessageKey, Entry>`, so a key missing from (or extra in)
 * either file is a compile error. Keys are flat dotted strings; see ./README.md.
 *
 * `msg.<code>` entries are the backend's stable message codes (one per entry in
 * services/admin_messages.py, with the same `{param}` names).
 */
export const en = {
  // App
  'app.title': 'Quberi Platform Admin',
  'app.brand': 'Quberi Admin',
  'app.loadingStats': 'Loading platform stats…',

  // Language switch
  'lang.label': 'Language',
  'lang.en': 'English',
  'lang.zh': '简体中文',

  // Shared
  'common.refresh': 'Refresh',
  'common.loading': 'Loading…',
  'common.cancel': 'Cancel',
  'common.save': 'Save',
  'common.saving': 'Saving…',
  'common.close': 'Close',
  'common.edit': 'Edit',
  'common.delete': 'Delete',
  'common.remove': 'Remove',
  'common.none': '(none)',

  // Navigation
  'nav.overview': 'Overview',
  'nav.organizations': 'Organizations',
  'nav.models': 'Models',
  'nav.clients': 'Outside tools',
  'nav.traffic': 'Traffic',
  'nav.signOut': 'Sign out',

  // Sign-in gate
  'gate.signIn.hint':
    'Sign in with a platform admin account. On a new deployment the first password is written once to the API log.',
  'gate.signIn.username': 'Username',
  'gate.signIn.password': 'Password',
  'gate.signIn.submit': 'Sign in',
  'gate.signIn.busy': 'Signing in…',
  'gate.signIn.tooMany': 'Too many attempts. Wait a few minutes and try again.',
  'gate.signIn.invalid': 'Invalid username or password.',
  'gate.signIn.unreachable': 'Could not reach the admin API. Try again in a moment.',

  // Forced password change
  'gate.password.title': 'Choose a new password',
  'gate.password.hint':
    'This account still uses a password it was given. Choose your own ({min} to {max} characters) to continue.',
  'gate.password.current': 'Current password',
  'gate.password.new': 'New password',
  'gate.password.confirm': 'Confirm new password',
  'gate.password.mismatch': 'The new passwords do not match.',
  'gate.password.failed': 'Could not change the password. Try again.',
  'gate.password.submit': 'Change password',

  // Request errors
  'error.generic': 'Something went wrong. Try again.',
  'error.server': 'The server ran into a problem. Try again in a moment.',
  'error.network': 'Could not reach the admin API. Try again in a moment.',
  'error.validation': 'Some of the values entered are not valid.',
  'error.status': 'Request failed with status {status}',

  // Overview
  'overview.title': 'Overview',
  'overview.kpi.organizations': 'Organizations',
  'overview.kpi.newOrgs': 'New orgs (24h / 7d)',
  'overview.kpi.identities': 'Identities (people)',
  'overview.kpi.activeOrgs': 'Active orgs (7d)',
  'overview.kpi.spaces': 'Spaces',
  'overview.kpi.documents': 'Documents',
  'overview.kpi.conversations': 'Conversations',
  'overview.kpi.messages': 'Messages',
  'overview.sub.organizations': '{personal} personal · {team} team',
  'overview.sub.newOrgs': '{n} in last 7 days',
  'overview.sub.identities': '{a} new in 24h · {b} in 7d',
  'overview.sub.activeOrgs': 'sent at least one message',
  'overview.sub.documents': '{n} indexed',
  'overview.trend.title': 'Signups — last 14 days',
  'overview.trend.aria': 'Daily signups, last 14 days',

  // Organizations
  'orgs.title': 'Organizations ({n})',
  'orgs.col.name': 'Name',
  'orgs.col.tier': 'Tier',
  'orgs.col.owner': 'Owner',
  'orgs.col.members': 'Members',
  'orgs.col.spaces': 'Spaces',
  'orgs.col.docs': 'Docs',
  'orgs.col.conversations': 'Conversations',
  'orgs.col.messages': 'Messages',
  'orgs.col.created': 'Created',
  'orgs.demo': 'demo',
  'orgs.tier.personal': 'personal',
  'orgs.tier.team': 'team',
  'orgs.empty': 'No organizations yet.',
  'orgs.drawer.title': 'Platform model visibility',
  'orgs.drawer.hint':
    "Controls whether this org's members see Quberi's platform-shared chat models (DeepSeek, OpenAI, ...) in their own model picker. Never affects the org's own BYOK models — those stay visible regardless of this setting.",
  'orgs.mode.all': 'All',
  'orgs.mode.allHint': 'Every platform model is visible (default).',
  'orgs.mode.none': 'None',
  'orgs.mode.noneHint': "Hide every platform model — only this org's own BYOK models show.",
  'orgs.mode.allowlist': 'Allowlist',
  'orgs.mode.allowlistHint': 'Only the models checked below are visible.',
  'orgs.noChatModels': 'No chat models in the registry.',

  // Models
  'models.title': 'Platform-shared models',
  'models.addKey': 'Add key',
  'models.hint':
    "Keys shown here are available to every organisation (subject to each org's own platform-model visibility setting). Several keys may share a vendor — the active one with the lowest priority number is used; pause a key to take it out of rotation without deleting it.",
  'models.tab.text': 'Text models',
  'models.tab.vision': 'Vision models',
  'models.visionHint':
    'Models that read pictures, on the same keys. An organisation chooses one of them in its own model settings and turns picture descriptions on; nothing is sent until it does. A model a key serves is available for chat and for pictures alike, so removing one here removes it everywhere. Add <code>deepseek-flash</code> to a DeepSeek key to offer it.',
  'models.col.name': 'Name',
  'models.col.baseUrl': 'Base URL',
  'models.col.priority': 'Priority',
  'models.col.status': 'Status',
  'models.col.models': 'Models',
  'models.col.actions': 'actions',
  'models.keyConfigured': 'Key configured',
  'models.noKey': 'No key',
  'models.status.active': 'active',
  'models.status.paused': 'paused',
  'models.status.error': 'error',
  'models.status.archived': 'archived',
  'models.empty': 'No platform-default bindings configured.',
  'models.confirmDelete': 'Delete platform key "{name}"? This cannot be undone.',
  'models.editor.addTitle': 'Add platform key',
  'models.editor.editTitle': 'Edit {name}',
  'models.editor.vendor': 'Vendor',
  'models.editor.vendorPlaceholder': 'e.g. deepseek, openai, dashscope',
  'models.editor.displayName': 'Display name',
  'models.editor.apiKey': 'API key',
  'models.editor.apiKeyKeep': 'API key (leave blank to keep current)',
  'models.editor.priority': 'Priority (lower tried first)',
  'models.editor.visionModels': 'Models that read pictures this key serves',
  'models.editor.embeddingModels': 'Embedding models this key serves',
  'models.editor.chatModels': 'Chat models this key serves',

  // Outside tools
  'clients.title': 'Outside tools',
  'clients.addTool': 'Add tool',
  'clients.hint':
    "The tools that may ask a person for access to their knowledge. Each one has the id it presents and the exact callback addresses it may be sent its code to: <code>loopback</code> means a callback on the person's own machine; anything else must be an exact <code>https://</code> address, and <code>{id}</code> stands for one short segment that must be the same in the tool's id and its callback. Nothing is fetched from a tool's URL.",
  'clients.advertising.on':
    'Client ID metadata documents: <strong>advertised</strong>. Tools such as Claude Code identify themselves by URL, so keep their entries switched on.',
  'clients.advertising.off':
    'Client ID metadata documents: <strong>not advertised</strong>. Switch on a tool that uses a metadata document URL to advertise it.',
  'clients.col.tool': 'Tool',
  'clients.col.clientId': 'Client id',
  'clients.col.sentTo': 'May be sent its code to',
  'clients.col.status': 'Status',
  'clients.col.actions': 'actions',
  'clients.kind.registered': 'Registered id',
  'clients.kind.metadata_url': 'Metadata document URL',
  'clients.mcpServer': 'MCP server',
  'clients.restOnly': 'REST API only',
  'clients.on': 'on',
  'clients.off': 'off',
  'clients.switchOff': 'Click to switch off',
  'clients.switchOn': 'Click to switch on',
  'clients.empty': 'No tools are allowed, so nobody can connect one.',
  'clients.confirmRemove':
    'Remove "{name}"? People can no longer connect it, and its existing connections stop refreshing.',
  'clients.refused.title': 'Recently refused',
  'clients.refused.hint':
    'Attempts by signed-in people that were turned away. A tool you want to allow shows up here with the id and callback it actually used.',
  'clients.refused.empty': 'Nothing has been refused lately.',
  'clients.refused.col.callback': 'Callback it asked for',
  'clients.refused.col.why': 'Why',
  'clients.refused.col.when': 'When',
  'clients.reason.unknown_client': 'Unknown tool',
  'clients.reason.redirect_not_allowed': 'Callback address not allowed',
  'clients.allow': 'Allow…',
  'clients.editor.addTitle': 'Allow a tool',
  'clients.editor.editTitle': 'Edit {name}',
  'clients.editor.name': 'Name shown to people',
  'clients.editor.kind': 'How the tool names itself',
  'clients.editor.sentTo': 'May be sent its code to (one per line)',
  'clients.editor.switchedOn': 'Switched on',
  'clients.editor.allowMcp': 'May use the MCP server',
  'clients.editor.allowMcpHint': 'Leave off for a tool that only uses the REST API.',
  'clients.editor.note': 'Note (optional)',

  // Traffic
  'traffic.hint':
    'Who is visiting, from the reverse proxy’s access log: real people against crawlers, scanners, MCP directories and MCP clients.',
  'traffic.window.aria': 'Time window',
  'traffic.exclude.label': 'Exclude my addresses',
  'traffic.exclude.apply': 'Apply',
  'traffic.exclude.hint':
    'Full addresses, comma-separated. Their requests are left out of every count. Saved in this browser only.',
  'traffic.loading': 'Loading traffic…',
  'traffic.disabled.title': 'Traffic is not set up',
  'traffic.disabled.turnOn': 'To turn it on:',
  'traffic.disabled.step1':
    'Mount the proxy’s log volume into the API container, read-only: <code>caddy-data:/caddy-data:ro</code>.',
  'traffic.disabled.step2':
    'Set <code>QUBERI_ACCESS_LOG_PATH</code> (for example <code>/caddy-data/access.log</code>) and <code>QUBERI_TRAFFIC_SITE_HOSTS</code> (the host names you serve, comma-separated).',
  'traffic.disabled.step3': 'Recreate the API container so it picks up the mount and the settings.',
  'traffic.disabled.footer':
    '<code>deploy/compose.prod.yaml</code> already does the first two for the production stack. See “Access log and the admin Traffic page” in <code>deploy/README.md</code>.',
  'traffic.reason.not_configured':
    'No access log is configured, so there is nothing to summarise yet.',
  'traffic.reason.file_missing':
    'An access log path is set, but there is no file there. The proxy’s log volume is probably not mounted into the API container.',
  'traffic.reason.unreadable':
    'The access log exists, but the API container is not allowed to read it. Check the file’s permissions.',
  'traffic.windowLine': {
    one: '{from} to {to} UTC · {n} log file read',
    other: '{from} to {to} UTC · {n} log files read',
  },
  'traffic.truncated':
    'The log is larger than the read limit, so only its newest part is included. Counts for this window are partial.',
  'traffic.kpi.requests': 'Requests',
  'traffic.kpi.visitors': 'Distinct visitors',
  'traffic.kpi.automatedShare': 'Automated share',
  'traffic.sub.byAddress': 'by address',
  'traffic.sub.automatedRequests': '{n} automated requests',
  'traffic.signals.title': 'Signs of real use (bots left out)',
  'traffic.kpi.guestQuestions': 'Guest questions',
  'traffic.kpi.signIn': 'Sign-in requests',
  'traffic.kpi.oauth': 'OAuth requests',
  'traffic.kpi.mcpCalls': 'MCP calls accepted',
  'traffic.sub.notBots': 'successful, not from bots',
  'traffic.sub.signIn': 'email or phone code',
  'traffic.sub.oauth': 'authorize or token',
  'traffic.chart.title.hour': 'Requests per hour (UTC)',
  'traffic.chart.title.day': 'Requests per day (UTC)',
  'traffic.chart.aria.hour': 'Requests per hour: {total} in total, {automated} automated',
  'traffic.chart.aria.day': 'Requests per day: {total} in total, {automated} automated',
  'traffic.chart.bar': '{key}: {requests} requests, {automated} automated',
  'traffic.legend.automated': 'Automated',
  'traffic.legend.rest': 'Everything else',
  'traffic.block.byKind': 'By kind of visitor',
  'traffic.block.bySite': 'By site',
  'traffic.block.byCountry': 'By country (top 10)',
  'traffic.block.byResponse': 'By response',
  'traffic.block.mcpWho': 'MCP: who is calling',
  'traffic.block.mcpWhat': 'MCP: what they ask for',
  'traffic.block.topVisitors': 'Top visitors',
  'traffic.block.webPages': 'Pages on the web site (browser-like and search engines)',
  'traffic.aria.byKind': 'Requests by kind of visitor',
  'traffic.aria.bySite': 'Requests by site',
  'traffic.aria.byCountry': 'Requests by country',
  'traffic.aria.byResponse': 'Requests by response class',
  'traffic.aria.mcpAgents': 'MCP user agents',
  'traffic.aria.mcpPaths': 'MCP paths',
  'traffic.aria.webPages': 'Web pages',
  'traffic.col.kind': 'Kind',
  'traffic.col.requests': 'Requests',
  'traffic.col.share': 'Share',
  'traffic.col.site': 'Site',
  'traffic.col.country': 'Country',
  'traffic.col.status': 'Status',
  'traffic.col.userAgent': 'User-Agent',
  'traffic.col.method': 'Method',
  'traffic.col.path': 'Path',
  'traffic.col.address': 'Address',
  'traffic.col.source': 'Source',
  'traffic.col.total': 'Total',
  'traffic.kind.direct_or_unknown_host.label': 'Direct or unknown host',
  'traffic.kind.direct_or_unknown_host.meaning':
    'Addressed to a bare IP or to a host that is not one of your sites. Scanners.',
  'traffic.kind.search_engine.label': 'Search engines',
  'traffic.kind.search_engine.meaning': 'Search engines and well-known crawlers that name themselves.',
  'traffic.kind.mcp_directory_or_probe.label': 'MCP directories and probes',
  'traffic.kind.mcp_directory_or_probe.meaning':
    'Directories, health checks and trust or ranking services.',
  'traffic.kind.scanner_or_tool.label': 'Scanners and tools',
  'traffic.kind.scanner_or_tool.meaning':
    'Command-line tools, HTTP libraries, security scanners, or no User-Agent.',
  'traffic.kind.mcp_client.label': 'MCP clients',
  'traffic.kind.mcp_client.meaning':
    'Tools connecting to the MCP endpoint (Codex, Claude, Cursor, ...).',
  'traffic.kind.browser_like.label': 'Browser-like',
  'traffic.kind.browser_like.meaning':
    'A browser User-Agent on one of your sites. It can still be a crawler.',
  'traffic.kind.other.label': 'Other',
  'traffic.kind.other.meaning': 'Anything the rules above did not match.',
  'traffic.site.unknown': '(unknown host)',
  'traffic.site.other': '(other host)',
  'traffic.unknown': 'unknown',
  'traffic.statusOther': 'other',
  'traffic.source.header': 'header',
  'traffic.source.remote_ip': 'remote_ip',
  'traffic.sourceTitle.header': 'Taken from a request header set by the CDN',
  'traffic.sourceTitle.remote_ip': 'The connecting address',
  'traffic.footer':
    'Kinds are inferred from the User-Agent and the request pattern; a browser-like visit can still be a crawler.',
  'traffic.empty': 'No requests in this window.',

  // Backend message codes
  'msg.admin.auth.invalid_token': 'Invalid platform admin token.',
  'msg.admin.auth.api_disabled': 'The platform admin API is disabled on this deployment.',
  'msg.admin.auth.missing_token': 'Missing bearer token.',
  'msg.admin.auth.sign_in_required': 'Sign in required.',
  'msg.admin.auth.password_too_short': 'Password must be at least {min} characters.',
  'msg.admin.auth.password_too_long': 'Password must be at most {max} characters.',
  'msg.admin.auth.password_edge_spaces': 'Password must not start or end with spaces.',
  'msg.admin.auth.password_repetitive': 'Password is too repetitive.',
  'msg.admin.auth.password_has_username': 'Password must not contain the username.',
  'msg.admin.auth.try_again_shortly': 'Try again shortly.',
  'msg.admin.auth.too_many_attempts': 'Too many attempts. Try again later.',
  'msg.admin.auth.bad_credentials': 'Invalid username or password.',
  'msg.admin.auth.current_password_wrong': 'The current password is incorrect.',
  'msg.admin.auth.password_unchanged': 'Choose a password different from the current one.',
  'msg.admin.oauth.client_id_taken': 'A tool with that client id is already configured.',
  'msg.admin.oauth.tool_not_found': 'No such tool.',
  'msg.admin.oauth.unknown_kind': 'Unknown kind.',
  'msg.admin.oauth.registered_id_format':
    'A registered client id is 1 to 64 letters, digits, dots, dashes or underscores.',
  'msg.admin.oauth.url_too_long': '{subject} is too long.',
  'msg.admin.oauth.url_wildcard_space': '{subject} may not contain wildcards or spaces.',
  'msg.admin.oauth.url_placeholder_not_allowed': '{subject} may not contain {placeholder}.',
  'msg.admin.oauth.url_placeholder_once':
    '{subject} may contain {placeholder} at most once.',
  'msg.admin.oauth.url_braces': '{subject} contains braces other than {placeholder}.',
  'msg.admin.oauth.url_invalid': '{subject} is not a valid address.',
  'msg.admin.oauth.url_not_https': '{subject} must be an https:// address.',
  'msg.admin.oauth.url_extras':
    '{subject} may not carry credentials, a query or a fragment.',
  'msg.admin.oauth.url_port': '{subject} may not name a port.',
  'msg.admin.oauth.url_domain':
    '{subject} must use a real domain name, not an IP address.',
  'msg.admin.oauth.url_placeholder_path':
    '{subject}: {placeholder} may only appear in the path.',
  'msg.admin.oauth.redirect_needs_id':
    'Redirect {rule} uses {placeholder} but the client id does not.',
  'msg.admin.oauth.redirect_required': 'At least one redirect rule is required.',
  'msg.admin.oauth.subject.client_id_url': 'The client id URL',
  'msg.admin.oauth.subject.redirect': 'Redirect {rule}',
  'msg.admin.models.binding_not_found': 'Platform key not found.',
  'msg.admin.models.visibility_mode_invalid': 'Invalid model visibility mode {mode}.',
  'msg.admin.models.org_not_found': 'Organization {id} not found.',
  'msg.admin.orgs.not_found': 'Organization not found: {id}',
  'msg.admin.traffic.address_note':
    "Addresses are masked. A visitor address taken from a request header can be forged by anyone who reaches the server directly, without going through the CDN; rows marked 'remote_ip' use the connecting address.",
} as const satisfies Dict
