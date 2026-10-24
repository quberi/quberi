/**
 * Starter questions offered to a guest on the chat page.
 *
 * Order = the order they are offered in. Data security is one of Quberi's
 * main selling points, so its questions come right after the basics.
 */
export const GUEST_STARTER_KEYS = [
  'what',
  'isolation',
  'visibility',
  'data',
  'cloud',
  'offboard',
  'connect',
  'start',
  'models',
  'share',
  'files',
  'agents',
  'audit',
  'guest',
] as const

/** How many suggestions are on screen at once. */
export const GUEST_STARTER_BATCH = 6

/**
 * The suggestions to show right now.
 *
 * ``remaining`` is every question that has **not** been asked yet, in offer
 * order. The window is ``size`` long and starts ``batch`` pages in, wrapping
 * past the end so "next batch" never runs dry:
 *
 * - asking one removes it from ``remaining``, so the ones after it slide up
 *   and a new one enters at the end - the strip stays full;
 * - "next batch" just increments ``batch``, no question needed first;
 * - with ``size`` or fewer left, they are all shown.
 */
export function pickStarters<T>(
  remaining: readonly T[],
  batch: number,
  size: number = GUEST_STARTER_BATCH,
): T[] {
  if (remaining.length === 0 || size <= 0) return []
  const count = Math.min(size, remaining.length)
  const start = (Math.max(batch, 0) * size) % remaining.length
  return Array.from({ length: count }, (_, i) => remaining[(start + i) % remaining.length])
}

/** One suggestion as the server ranks it. */
export interface RankedStarter {
  key: string
  /** Among the most-asked recently, with enough asks to mean something. */
  hot: boolean
}

/**
 * The server's ranking merged with what this build of the app can display.
 *
 * - keys the server ranks come first, in its order (most-asked first);
 * - a key this app has no text for is dropped rather than shown as a raw id
 *   (the server may be newer than the page the visitor has open);
 * - a key this app knows but the server didn't send is appended in the
 *   curated order, so a missing or partial response never hides questions.
 *
 * With no server data at all this is just the curated order.
 */
export function orderStarters(
  ranked: readonly RankedStarter[] | null | undefined,
): RankedStarter[] {
  const known = new Set<string>(GUEST_STARTER_KEYS)
  const out: RankedStarter[] = []
  const seen = new Set<string>()
  for (const r of ranked ?? []) {
    if (known.has(r.key) && !seen.has(r.key)) {
      seen.add(r.key)
      out.push({ key: r.key, hot: r.hot })
    }
  }
  for (const key of GUEST_STARTER_KEYS) {
    if (!seen.has(key)) out.push({ key, hot: false })
  }
  return out
}
