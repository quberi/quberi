import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AdminApiError, adminLogin, deleteOAuthClient, getOverview } from './api'
import { LANG_STORAGE_KEY, setLang } from './i18n'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

let fetchMock: ReturnType<typeof vi.fn>

function lastHeaders(): Record<string, string> {
  return (fetchMock.mock.calls.at(-1)?.[1] as RequestInit).headers as Record<string, string>
}

beforeEach(() => {
  window.localStorage.removeItem(LANG_STORAGE_KEY)
  setLang('en')
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => {
  vi.unstubAllGlobals()
  window.localStorage.removeItem(LANG_STORAGE_KEY)
})

describe('Accept-Language', () => {
  it('follows the active language on adminFetch and adminLogin', async () => {
    fetchMock.mockImplementation(async () => jsonResponse(200, { token: 't' }))

    await getOverview('tok')
    expect(lastHeaders()['Accept-Language']).toBe('en-US')
    await adminLogin('admin', 'pw')
    expect(lastHeaders()['Accept-Language']).toBe('en-US')

    setLang('zh')
    await getOverview('tok')
    expect(lastHeaders()['Accept-Language']).toBe('zh-CN')
    await adminLogin('admin', 'pw')
    expect(lastHeaders()['Accept-Language']).toBe('zh-CN')
    // The bearer token and other headers are unchanged.
    await getOverview('tok')
    expect(lastHeaders().Authorization).toBe('Bearer tok')
    expect(lastHeaders().Accept).toBe('application/json')
  })
})

describe('error bodies', () => {
  it('reads detail, code and params into AdminApiError', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(400, {
        detail: 'password must be at least 12 characters',
        code: 'admin.auth.password_too_short',
        params: { min: 12 },
      }),
    )
    const err = await getOverview('tok').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(AdminApiError)
    const apiError = err as AdminApiError
    expect(apiError.status).toBe(400)
    expect(apiError.message).toBe('password must be at least 12 characters')
    expect(apiError.detail).toBe('password must be at least 12 characters')
    expect(apiError.code).toBe('admin.auth.password_too_short')
    expect(apiError.params).toEqual({ min: 12 })
  })

  it('reads the same body from adminLogin', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(401, { detail: 'invalid username or password', code: 'admin.auth.bad_credentials' }),
    )
    const err = (await adminLogin('a', 'b').catch((e: unknown) => e)) as AdminApiError
    expect(err.status).toBe(401)
    expect(err.code).toBe('admin.auth.bad_credentials')
    expect(err.params).toBeUndefined()
  })

  it('turns a 422 list detail into the validation code, never "[object Object]"', async () => {
    const body = { detail: [{ loc: ['body', 'name'], msg: 'field required', type: 'missing' }] }
    fetchMock.mockResolvedValue(jsonResponse(422, body))
    const err = (await getOverview('tok').catch((e: unknown) => e)) as AdminApiError
    expect(err.status).toBe(422)
    expect(err.code).toBe('validation')
    expect(err.detail).toBe('')
    expect(err.message).not.toContain('[object Object]')

    fetchMock.mockResolvedValue(jsonResponse(422, body))
    const loginErr = (await adminLogin('a', 'b').catch((e: unknown) => e)) as AdminApiError
    expect(loginErr.code).toBe('validation')
    expect(loginErr.message).not.toContain('[object Object]')
  })

  it('copes with a body that is not JSON', async () => {
    fetchMock.mockResolvedValue(new Response('<html>Bad gateway</html>', { status: 502 }))
    const err = (await getOverview('tok').catch((e: unknown) => e)) as AdminApiError
    expect(err.status).toBe(502)
    expect(err.detail).toBe('')
    expect(err.code).toBeNull()
    expect(err.message).toBe('Request failed with status 502')
  })

  it('keeps the two-argument construction working', () => {
    const err = new AdminApiError(401, 'sign in required')
    expect(err.status).toBe(401)
    expect(err.message).toBe('sign in required')
    expect(err.code).toBeNull()
    expect(err.params).toBeUndefined()
  })
})

describe('success responses', () => {
  it('returns undefined for a 204', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))
    await expect(deleteOAuthClient('tok', 'abc')).resolves.toBeUndefined()
  })

  it('returns the parsed body otherwise', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { new_organizations_24h: 3 }))
    await expect(getOverview('tok')).resolves.toEqual({ new_organizations_24h: 3 })
  })
})
