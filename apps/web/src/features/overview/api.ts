import { apiFetch } from '../api/client'
import type { DevCredentials } from '../auth/types'
import type { Overview } from './types'

/** The viewer's UTC offset in minutes, east positive, so the server buckets days like they do. */
export function viewerTzOffset(): number {
  return -new Date().getTimezoneOffset()
}

export function getOverview(credentials: DevCredentials): Promise<Overview> {
  return apiFetch<Overview>(`/api/v1/organizations/current/overview?tz_offset=${viewerTzOffset()}`, {
    credentials,
  })
}
