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

function lines() {
  return Array.from(document.querySelectorAll('.oauth-perms li')).map((li) => li.textContent)
}

describe('consent page copy follows the requested scope', () => {
  it('read-only: one ability line and the original "cannot" wording', () => {
    renderCard('knowledge:read')
    expect(lines()).toEqual(['oauth.canRead', 'oauth.cannot'])
  })

  it('read + save: says it can save, and drops the "can\'t upload" promise', () => {
    renderCard('knowledge:read knowledge:write')
    expect(lines()).toEqual(['oauth.canRead', 'oauth.canSave', 'oauth.cannotWriteOrUpload'])
  })

  it('read + upload: says it can upload', () => {
    renderCard('knowledge:read knowledge:upload')
    expect(lines()).toEqual(['oauth.canRead', 'oauth.canUpload', 'oauth.cannotWriteOrUpload'])
  })

  it('all three: one line for each power', () => {
    renderCard('knowledge:read knowledge:write knowledge:upload')
    expect(lines()).toEqual([
      'oauth.canRead',
      'oauth.canSave',
      'oauth.canUpload',
      'oauth.cannotWriteOrUpload',
    ])
  })

  it('upload ONLY: never claims it can read, and never promises it cannot upload', () => {
    renderCard('knowledge:upload')
    expect(lines()).toEqual(['oauth.canUpload', 'oauth.cannotWriteOrUpload'])
    expect(screen.queryByText('oauth.cannot')).toBeNull()
    expect(screen.queryByText('oauth.canRead')).toBeNull()
  })

  it('save ONLY: says it can save and nothing about reading', () => {
    renderCard('knowledge:write')
    expect(lines()).toEqual(['oauth.canSave', 'oauth.cannotWriteOrUpload'])
  })

  it('does not mistake a look-alike scope for a real one', () => {
    renderCard('knowledge:writeonly knowledge:uploads knowledge:read')
    expect(lines()).toEqual(['oauth.canRead', 'oauth.cannot'])
  })

  it('never shows the read-only "cannot upload" promise when a change power is asked for', () => {
    for (const scope of [
      'knowledge:write',
      'knowledge:upload',
      'knowledge:read knowledge:write',
      'knowledge:read knowledge:upload',
      'knowledge:read knowledge:write knowledge:upload',
    ]) {
      const { unmount } = renderCard(scope)
      expect(screen.queryByText('oauth.cannot')).toBeNull()
      unmount()
    }
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
      for (const key of ['canRead', 'canSave', 'canUpload', 'cannotWriteOrUpload', 'uploadBadge']) {
        expect(o[key], `${name} is missing oauth.${key}`).toBeTruthy()
      }
      // The sentence a read-only person has always seen must not change.
      expect(o.canRead).toBe(o.can)
      // Uploading is named in words, in the language of the page.
      expect(o.canUpload).toMatch(name === 'en-US' ? /files/i : /文件/)
      // The "cannot" sentence for change powers warns that content is shared.
      expect(o.cannotWriteOrUpload).toMatch(name === 'en-US' ? /others with access/i : /其他人/)
      expect(o.cannotWriteOrUpload).not.toMatch(name === 'en-US' ? /can't upload/i : /不能上传/)
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
    expect(screen.queryByText('oauth.uploadBadge')).toBeNull()
    const badge = screen.getByText('oauth.writeBadge')
    const writerRow = screen.getByText('Writer tool').closest('li')
    const readerRow = screen.getByText('Reader tool').closest('li')
    expect(writerRow?.contains(badge)).toBe(true)
    expect(readerRow?.contains(badge)).toBe(false)
  })
})


describe('account page marks tools that can upload files', () => {
  it('shows the upload badge only on a grant that holds the upload scope', async () => {
    listExternalAccess.mockResolvedValue({
      items: [
        {
          id: 'a',
          client_name: 'Saver',
          organization_id: 'o',
          organization_name: 'Me',
          scope: 'knowledge:read knowledge:write',
          created_at: '2026-10-15T00:00:00Z',
          last_used_at: null,
          expires_at: '2027-01-15T00:00:00Z',
        },
        {
          id: 'b',
          client_name: 'Uploader',
          organization_id: 'o',
          organization_name: 'Me',
          scope: 'knowledge:read knowledge:upload',
          created_at: '2026-10-15T00:00:00Z',
          last_used_at: null,
          expires_at: '2027-01-15T00:00:00Z',
        },
      ],
    })
    render(<ConnectedTools />)
    await waitFor(() => expect(screen.getByText('Uploader')).toBeTruthy())
    expect(screen.getAllByText('oauth.uploadBadge')).toHaveLength(1)
    const uploaderRow = screen.getByText('Uploader').closest('li')
    const saverRow = screen.getByText('Saver').closest('li')
    expect(uploaderRow?.contains(screen.getByText('oauth.uploadBadge'))).toBe(true)
    expect(saverRow?.contains(screen.getByText('oauth.uploadBadge'))).toBe(false)
  })
})

describe('account page tells apart tools that look the same', () => {
  it('shows the self-reported caller next to the name, only where there is one', async () => {
    const base = {
      client_name: 'MCP client',
      organization_id: 'o',
      organization_name: 'Me',
      scope: 'knowledge:read',
      created_at: '2026-10-15T00:00:00Z',
      last_used_at: null,
      expires_at: '2027-01-15T00:00:00Z',
    }
    listExternalAccess.mockResolvedValue({
      items: [
        { ...base, id: 'a', caller_label: 'Codex 0.42' },
        { ...base, id: 'b', caller_label: 'Claude Desktop 1.2' },
        { ...base, id: 'c', caller_label: null },
      ],
    })
    render(<ConnectedTools />)
    await waitFor(() => expect(screen.getByText('Codex 0.42')).toBeTruthy())
    expect(screen.getByText('Claude Desktop 1.2')).toBeTruthy()
    const rows = screen.getAllByText('MCP client').map((el) => el.closest('li'))
    expect(rows).toHaveLength(3)
    expect(rows[2]?.querySelector('.connected-tool-caller')).toBeNull()
    expect(screen.getAllByText('connectedTools.authorizedAt')).toHaveLength(3)
  })
})
