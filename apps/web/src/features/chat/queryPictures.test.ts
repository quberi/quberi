import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  MAX_KEPT_QUERY_PICTURES,
  ensureQueryPicture,
  forgetQueryPictures,
  rememberQueryPicture,
  useQueryPicture,
} from './queryPictures'

describe('query pictures kept in the tab', () => {
  let counter = 0
  const revoked: string[] = []

  beforeEach(() => {
    counter = 0
    revoked.length = 0
    vi.stubGlobal('URL', {
      createObjectURL: () => `blob:test/${++counter}`,
      revokeObjectURL: (url: string) => revoked.push(url),
    })
  })

  afterEach(() => {
    forgetQueryPictures()
    vi.unstubAllGlobals()
  })

  const file = () => new File(['x'], 'a.png', { type: 'image/png' })

  it('shows a remembered picture for its message only', () => {
    const { result } = renderHook(() => ({
      mine: useQueryPicture('m1'),
      other: useQueryPicture('m2'),
    }))
    expect(result.current.mine).toBeUndefined()
    act(() => {
      rememberQueryPicture('m1', file())
    })
    expect(result.current.mine).toBe('blob:test/1')
    expect(result.current.other).toBeUndefined()
  })

  it('releases the old URL when a message gets a new picture', () => {
    act(() => {
      rememberQueryPicture('m1', file())
      rememberQueryPicture('m1', file())
    })
    expect(revoked).toEqual(['blob:test/1'])
  })

  it('lets the oldest go past the cap', () => {
    act(() => {
      for (let i = 0; i <= MAX_KEPT_QUERY_PICTURES; i++) rememberQueryPicture(`m${i}`, file())
    })
    expect(revoked).toEqual(['blob:test/1'])
    const { result } = renderHook(() => useQueryPicture('m0'))
    expect(result.current).toBeUndefined()
  })

  it('does not throw when the browser cannot make a URL', () => {
    vi.stubGlobal('URL', {
      createObjectURL: () => {
        throw new Error('unsupported')
      },
      revokeObjectURL: () => undefined,
    })
    expect(rememberQueryPicture('m1', file())).toBeUndefined()
  })

  it('fetches a kept picture once and then shows it', async () => {
    const load = vi.fn().mockResolvedValue(new Blob(['x']))
    const { result } = renderHook(() => useQueryPicture('m1'))
    await act(async () => {
      await Promise.all([ensureQueryPicture('m1', load), ensureQueryPicture('m1', load)])
    })
    await act(async () => {
      await ensureQueryPicture('m1', load)
    })
    expect(load).toHaveBeenCalledTimes(1)
    expect(result.current).toBe('blob:test/1')
  })

  it('does not ask again after a failed fetch, and shows nothing', async () => {
    const load = vi.fn().mockRejectedValue(new Error('404'))
    const { result } = renderHook(() => useQueryPicture('m1'))
    await act(async () => {
      await ensureQueryPicture('m1', load)
      await ensureQueryPicture('m1', load)
    })
    expect(load).toHaveBeenCalledTimes(1)
    expect(result.current).toBeUndefined()
  })

  it('throws away a fetch that finishes after sign-out', async () => {
    let finish: (b: Blob) => void = () => undefined
    const load = () => new Promise<Blob>((resolve) => { finish = resolve })
    const { result } = renderHook(() => useQueryPicture('m1'))
    const pending = ensureQueryPicture('m1', load)
    act(() => {
      forgetQueryPictures()
    })
    await act(async () => {
      finish(new Blob(['x']))
      await pending
    })
    expect(result.current).toBeUndefined()
    expect(revoked).toEqual([])
  })

  it('forgets everything on sign-out', () => {
    const { result } = renderHook(() => useQueryPicture('m1'))
    act(() => {
      rememberQueryPicture('m1', file())
    })
    act(() => {
      forgetQueryPictures()
    })
    expect(result.current).toBeUndefined()
    expect(revoked).toEqual(['blob:test/1'])
  })
})
