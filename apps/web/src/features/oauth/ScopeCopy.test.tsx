import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import enUS from '../../i18n/locales/en-US.json'
import zhCN from '../../i18n/locales/zh-CN.json'
import { ConnectedTools } from './ConnectedTools'
import { ConsentCard } from './ConsentCard'
import type { ConsentView, ExternalGrant } from './api'

/**
 * Write is a larger power than search, so what the person reads before
 * clicking Allow must follow the scope the tool asked for - in both languages -
 * and the page must never promise "it can't upload" to someone granting write.
 */

const listExternalAccess = vi.fn()

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, string>) =>
      opts?.client ? `${key}:${opts.client}` : key,
    i18n: { resolvedLanguage: 'en-US', changeLanguage: () => undefined },
  }),
}))
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ credentials: { sessionToken: 't' } }),
}))
vi.mock('../../i18n/formatDate', () => ({ useFormatDate: () => (v: string) => v }))
vi.mock('./api', () => ({
  listExternalAccess: (...a: unknown[]) => listExternalAccess(...a),
  revokeExternalAccess: vi.fn(),
}))

function view(scope: string): ConsentView {
  return {
    client_id: 'quberi-harness',
    client_name: 'DeepSeek Harness',
    scope,
    redirect_uri: 'http://127.0.0.1:53682/callback',
    organizations: [
      {
        organization_id: 'o-me',
        name: 'Me',
        tier: 'personal',
        is_current: true,
        external_tools_enabled: true,
        can_enable: false,
      },
    ],
  }
}

function renderCard(scope: string) {
  return render(
    <ConsentCard
      busy={false}
      error={null}
      onCancel={() => undefined}
      onSelect={() => undefined}
      onAllow={() => undefined}
      onTurnOn={() => undefined}
      selectedOrgId="o-me"
      view={view(scope)}
    />,
  )
}

describe('consent page copy follows the requested scope', () => {
  it('read-only: keeps the original wording', () => {
    renderCard('knowledge:read')
    expect(screen.getByText('oauth.can')).toBeTruthy()
    expect(screen.getByText('oauth.cannot')).toBeTruthy()
    expect(screen.queryByText('oauth.canWrite')).toBeNull()
    expect(screen.queryByText('oauth.cannotWrite')).toBeNull()
  })

  it('write: says it can save notes, and drops the "can\'t upload" promise', () => {
    renderCard('knowledge:read knowledge:write')
    expect(screen.getByText('oauth.canWrite')).toBeTruthy()
    expect(screen.getByText('oauth.cannotWrite')).toBeTruthy()
    expect(screen.queryByText('oauth.can')).toBeNull()
    expect(screen.queryByText('oauth.cannot')).toBeNull()
  })

  it('write-only is treated as write too', () => {
    renderCard('knowledge:write')
    expect(screen.getByText('oauth.canWrite')).toBeTruthy()
  })

  it('does not mistake a look-alike scope for write', () => {
    renderCard('knowledge:writeonly knowledge:read')
    expect(screen.getByText('oauth.can')).toBeTruthy()
  })
})

describe('the real wording, in both languages', () => {
  for (const [name, locale] of [
    ['en-US', enUS],
    ['zh-CN', zhCN],
  ] as const) {
    it(`${name}: read wording denies uploading; write wording does not claim that`, () => {
      const o = locale.oauth as unknown as Record<string, string>
      expect(o.cannot).toBeTruthy()
      expect(o.canWrite).toBeTruthy()
      expect(o.cannotWrite).toBeTruthy()
      expect(o.writeBadge).toBeTruthy()
      // The sentence a person sees when granting write must not tell them it can't upload.
      const deniesUpload = name === 'en-US' ? /can't upload/i : /不能上传/
      expect(o.cannot).toMatch(deniesUpload)
      expect(o.cannotWrite).not.toMatch(deniesUpload)
      // And it must warn that saved notes are visible to others in that space.
      const warnsShared = name === 'en-US' ? /others with access/i : /其他人/
      expect(o.cannotWrite).toMatch(warnsShared)
      // The audit sentence now covers saved notes.
      expect(o.leaves).toMatch(name === 'en-US' ? /saved note/i : /保存的笔记/)
    })
  }
})

describe('account page marks tools that can save notes', () => {
  beforeEach(() => listExternalAccess.mockReset())

  function grant(over: Partial<ExternalGrant>): ExternalGrant {
    return {
      id: 'g1',
      client_name: 'DeepSeek Harness',
      organization_id: 'o',
      organization_name: 'Me',
      scope: 'knowledge:read',
      created_at: '2026-10-15T00:00:00Z',
      last_used_at: null,
      expires_at: '2027-01-15T00:00:00Z',
      ...over,
    }
  }

  it('shows the badge only on a grant that can write', async () => {
    listExternalAccess.mockResolvedValue({
      items: [
        grant({ id: 'r', client_name: 'Reader tool', scope: 'knowledge:read' }),
        grant({ id: 'w', client_name: 'Writer tool', scope: 'knowledge:read knowledge:write' }),
      ],
    })
    render(<ConnectedTools />)
    await waitFor(() => expect(screen.getByText('Writer tool')).toBeTruthy())
    expect(screen.getAllByText('oauth.writeBadge')).toHaveLength(1)
    const badge = screen.getByText('oauth.writeBadge')
    const writerRow = screen.getByText('Writer tool').closest('li')
    const readerRow = screen.getByText('Reader tool').closest('li')
    expect(writerRow?.contains(badge)).toBe(true)
    expect(readerRow?.contains(badge)).toBe(false)
  })
})
