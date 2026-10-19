import i18next from 'i18next'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  SESSION_INVALID_EVENT,
  apiErrorFrom,
  apiFetch,
  notifyIfSessionInvalid,
} from './client'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function listen() {
  const heard = vi.fn()
  window.addEventListener(SESSION_INVALID_EVENT, heard)
  return { heard, stop: () => window.removeEventListener(SESSION_INVALID_EVENT, heard) }
}

describe('session-ended signal', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('is raised only for a 401 that carries the session_invalid code', () => {
    const { heard, stop } = listen()
    notifyIfSessionInvalid(401, 'session_invalid')
    expect(heard).toHaveBeenCalledTimes(1)
    notifyIfSessionInvalid(401, undefined)
    notifyIfSessionInvalid(401, 'account_unavailable')
    notifyIfSessionInvalid(403, 'session_invalid')
    expect(heard).toHaveBeenCalledTimes(1)
    stop()
  })

  it('apiFetch raises it and still throws a readable error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(401, { detail: 'session ended', code: 'session_invalid' })),
    )
    const { heard, stop } = listen()
    const failure = await apiFetch('/api/v1/x').catch((e: unknown) => e)
    expect(failure).toBeInstanceOf(ApiError)
    expect((failure as ApiError).status).toBe(401)
    expect((failure as ApiError).code).toBe('session_invalid')
    expect((failure as ApiError).detail).toBe('session ended')
    expect(heard).toHaveBeenCalledTimes(1)
    stop()
  })

  it('a wrong sign-in code (401 without the code) does not sign anyone out', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(401, { detail: 'invalid code' })))
    const { heard, stop } = listen()
    await expect(apiFetch('/api/v1/account/email/verify')).rejects.toMatchObject({
      status: 401,
      detail: 'invalid code',
    })
    expect(heard).not.toHaveBeenCalled()
    stop()
  })

  it('apiErrorFrom covers hand-made requests too, and falls back to the given text', async () => {
    const { heard, stop } = listen()
    const opened = await apiErrorFrom(
      jsonResponse(401, { detail: 'x', code: 'session_invalid' }),
      'stream open failed',
    )
    expect(opened.code).toBe('session_invalid')
    expect(heard).toHaveBeenCalledTimes(1)
    const empty = await apiErrorFrom(new Response('not json', { status: 500 }), 'stream open failed')
    expect(empty.detail).toBe('stream open failed')
    expect(empty.code).toBeUndefined()
    expect(heard).toHaveBeenCalledTimes(1)
    stop()
  })
})

describe('a detail that is not text', () => {
  const validation = [
    { type: 'missing', loc: ['body', 'name'], msg: 'Field required', input: {} },
    { type: 'string_too_short', loc: ['body', 'slug'], msg: 'String should have at least 3 characters' },
  ]

  afterEach(async () => {
    vi.unstubAllGlobals()
    if (i18next.isInitialized) await i18next.changeLanguage('en-US')
  })

  it('shows one generic line, not the JSON, and keeps the status and the original detail', async () => {
    const failure = await apiErrorFrom(jsonResponse(422, { detail: validation }), 'fallback text')
    expect(failure.status).toBe(422)
    expect(failure.rawDetail).toEqual(validation)
    for (const shown of [failure.detail, failure.message]) {
      expect(shown).not.toContain('{')
      expect(shown).not.toContain('Field required')
      expect(shown).not.toContain('loc')
    }
    expect(failure.detail).not.toBe('')
    expect(failure.detail).not.toBe('fallback text')
  })

  it('goes through apiFetch too', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(422, { detail: validation })))
    const failure = (await apiFetch('/api/v1/x', { method: 'POST', body: {} }).catch(
      (e: unknown) => e,
    )) as ApiError
    expect(failure).toBeInstanceOf(ApiError)
    expect(failure.status).toBe(422)
    expect(failure.detail).not.toContain('{')
    expect(failure.rawDetail).toEqual(validation)
  })

  it('is worded in the chosen language', async () => {
    await i18next.init({
      lng: 'zh-CN',
      resources: {
        'zh-CN': { translation: { common: { requestFailed: '请求失败,请检查填写的内容后重试。' } } },
        'en-US': { translation: { common: { requestFailed: 'The request failed.' } } },
      },
    })
    expect((await apiErrorFrom(jsonResponse(422, { detail: validation }))).detail).toBe(
      '请求失败,请检查填写的内容后重试。',
    )
    await i18next.changeLanguage('en-US')
    expect((await apiErrorFrom(jsonResponse(422, { detail: validation }))).detail).toBe(
      'The request failed.',
    )
  })

  it('leaves a text detail, a missing detail and the fallback as they were', async () => {
    const text = await apiErrorFrom(jsonResponse(409, { detail: 'name taken' }), 'fb')
    expect(text.detail).toBe('name taken')
    expect(text.rawDetail).toBeUndefined()
    const none = await apiErrorFrom(jsonResponse(500, {}), 'fb')
    expect(none.detail).toBe('fb')
    expect(none.rawDetail).toBeUndefined()
  })
})
