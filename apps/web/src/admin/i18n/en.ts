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

  // Backend message codes
  'msg.admin.auth.invalid_token': 'Invalid platform admin token.',
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
