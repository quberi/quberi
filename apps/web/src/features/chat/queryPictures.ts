import { useSyncExternalStore } from 'react'

/**
 * Pictures people searched with, ready to show in the thread.
 *
 * The server keeps the picture for the person who searched (see
 * ``GET /conversations/{id}/messages/{message}/picture``). This holds the
 * browser's copy as an object URL, keyed by the id of the message it belongs
 * to: the file just sent is put here at once, so it shows without a download,
 * and a picture kept earlier is fetched the first time its question is shown.
 * Everything is released on sign-out.
 */

/** Pictures kept at once; the oldest is let go (and its URL released) past this. */
export const MAX_KEPT_QUERY_PICTURES = 24

const urls = new Map<string, string>()
// Loads in flight, and loads that failed (not retried until the next sign-in).
const loading = new Set<string>()
const failedLoads = new Set<string>()
// Bumped on sign-out so a load that finishes afterwards is thrown away.
let generation = 0
const listeners = new Set<() => void>()
// A new object each change, so ``useSyncExternalStore`` sees the update.
let snapshot: ReadonlyMap<string, string> = new Map()

function publish() {
  snapshot = new Map(urls)
  listeners.forEach((fn) => fn())
}

/** Hold ``picture`` for ``messageId`` and return the URL that shows it (undefined if the browser cannot make one). */
export function rememberQueryPicture(messageId: string, picture: Blob): string | undefined {
  const previous = urls.get(messageId)
  if (previous) URL.revokeObjectURL(previous)
  urls.delete(messageId)
  let url: string
  try {
    url = URL.createObjectURL(picture)
  } catch {
    // Showing the picture back is a courtesy; it must never fail the search.
    publish()
    return undefined
  }
  urls.set(messageId, url)
  while (urls.size > MAX_KEPT_QUERY_PICTURES) {
    const oldest = urls.keys().next().value as string
    URL.revokeObjectURL(urls.get(oldest) as string)
    urls.delete(oldest)
  }
  publish()
  return url
}

/**
 * Fetch the picture kept for ``messageId`` unless it is already here, on its
 * way, or failed once. Failures are quiet: the question just shows without it.
 */
export async function ensureQueryPicture(messageId: string, load: () => Promise<Blob>): Promise<void> {
  if (urls.has(messageId) || loading.has(messageId) || failedLoads.has(messageId)) return
  const started = generation
  loading.add(messageId)
  try {
    const blob = await load()
    if (started === generation) rememberQueryPicture(messageId, blob)
  } catch {
    if (started === generation) failedLoads.add(messageId)
  } finally {
    if (started === generation) loading.delete(messageId)
  }
}

/** Let every held picture go (signing out, or the chat closing). */
export function forgetQueryPictures(): void {
  generation += 1
  loading.clear()
  failedLoads.clear()
  if (urls.size === 0) return
  urls.forEach((url) => URL.revokeObjectURL(url))
  urls.clear()
  publish()
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/** The kept picture URL for a message, or undefined when there is none here. */
export function useQueryPicture(messageId: string): string | undefined {
  const all = useSyncExternalStore(
    subscribe,
    () => snapshot,
    () => snapshot,
  )
  return all.get(messageId)
}
