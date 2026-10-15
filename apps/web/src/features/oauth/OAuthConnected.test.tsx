import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { OAuthConnected, isConnectedRoute, readOutcome } from './OAuthConnected'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { resolvedLanguage: 'en-US', changeLanguage: () => undefined },
  }),
}))

afterEach(() => {
  vi.useRealTimers()
})

describe('result page address', () => {
  it('recognises its own path', () => {
    expect(isConnectedRoute('/oauth/connected')).toBe(true)
    expect(isConnectedRoute('/oauth/connected/')).toBe(true)
    expect(isConnectedRoute('/oauth/authorize')).toBe(false)
  })

  it('reads only one of a fixed set of outcome words', () => {
    expect(readOutcome('?status=ok')).toBe('ok')
    expect(readOutcome('?status=cancelled')).toBe('cancelled')
    expect(readOutcome('?status=org_off')).toBe('org_off')
    expect(readOutcome('?status=failed')).toBe('failed')
  })

  it('treats anything else as a failure, so a crafted link cannot choose what is said', () => {
    for (const q of ['', '?status=', '?status=success', '?status=OK', '?status=<script>alert(1)</script>', '?status=ok%00', '?other=ok']) {
      expect(readOutcome(q), q).toBe('failed')
    }
  })
})

describe('OAuthConnected', () => {
  it('says "connected" and offers nothing to click', () => {
    render(<OAuthConnected search="?status=ok" close={vi.fn()} />)
    expect(screen.getByText('oauth.result.ok.title')).toBeInTheDocument()
    expect(screen.getByText('oauth.result.closeHint')).toBeInTheDocument()
    // Nothing here can take the person anywhere or do anything: the only
    // control is the language switch.
    expect(screen.queryByRole('link')).toBeNull()
    const buttons = screen.getAllByRole('button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0]?.className).toContain('signin-lang')
  })

  it('explains each way it can go wrong, with its own text', () => {
    for (const status of ['cancelled', 'org_off', 'failed']) {
      const { unmount } = render(<OAuthConnected search={`?status=${status}`} close={vi.fn()} />)
      expect(screen.getByText(`oauth.result.${status}.title`)).toBeInTheDocument()
      unmount()
    }
  })

  it('tries to close its own tab shortly after success, and never after a failure', () => {
    vi.useFakeTimers()
    const close = vi.fn()
    const { unmount } = render(<OAuthConnected search="?status=ok" close={close} />)
    expect(close).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(3000)
    })
    expect(close).toHaveBeenCalledTimes(1)
    unmount()

    const stay = vi.fn()
    render(<OAuthConnected search="?status=failed" close={stay} />)
    act(() => {
      vi.advanceTimersByTime(10_000)
    })
    expect(stay).not.toHaveBeenCalled()
  })
})
