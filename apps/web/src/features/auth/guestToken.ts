/**
 * A random value that identifies *this browser* to the guest sandbox.
 *
 * Guests have no account, so this is how the app tells "a file I uploaded" from
 * "a file someone else uploaded": the browser makes up a long random token, keeps
 * it in localStorage and sends it as ``X-Guest-Token``. The server stores only its
 * SHA-256, so the token can't be read back from the database.
 *
 * It identifies a browser, not a person: clearing site data, using a private
 * window or another device means a fresh token (and the earlier uploads are no
 * longer "yours" until they expire). It is not authentication.
 */

const KEY = 'quberi.guestToken'

let memory: string | null = null

function generate(): string {
  const bytes = new Uint8Array(24) // 24 bytes -> 32 URL-safe chars
  if (typeof crypto !== 'undefined' && 'getRandomValues' in crypto) {
    crypto.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256)
  }
  let binary = ''
  bytes.forEach((b) => {
    binary += String.fromCharCode(b)
  })
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** Valid tokens are 22-128 URL-safe characters (matches the server). */
export function isValidToken(value: string | null | undefined): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{22,128}$/.test(value)
}

/** The token for this browser, created on first use. */
export function getGuestToken(): string {
  if (memory) return memory
  try {
    const stored = window.localStorage.getItem(KEY)
    if (isValidToken(stored)) {
      memory = stored
      return stored
    }
  } catch {
    /* storage blocked: fall through to an in-memory token */
  }
  const fresh = generate()
  memory = fresh
  try {
    window.localStorage.setItem(KEY, fresh)
  } catch {
    /* private mode: the token lives for this page load only */
  }
  return fresh
}
