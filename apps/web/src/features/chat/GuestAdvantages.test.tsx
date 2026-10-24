import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import enUS from '../../i18n/locales/en-US.json'
import zhCN from '../../i18n/locales/zh-CN.json'

import { GUEST_ADVANTAGES, GuestAdvantages } from './GuestAdvantages'
import { GUEST_STARTER_KEYS } from './starters'

const state = vi.hoisted(() => ({ locale: 'zh-CN', openAuth: vi.fn() }))

const bundles = { 'zh-CN': zhCN, 'en-US': enUS } as const
type Bundle = Record<string, unknown>

function lookup(bundle: Bundle, key: string): string {
  let node: unknown = bundle
  for (const part of key.split('.')) node = (node as Bundle | undefined)?.[part]
  return typeof node === 'string' ? node : key
}

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => lookup(bundles[state.locale as keyof typeof bundles], key),
    i18n: { resolvedLanguage: state.locale },
  }),
}))
vi.mock('../auth/AuthModalContext', () => ({ useAuthModal: () => ({ openAuth: state.openAuth }) }))

beforeEach(() => {
  state.locale = 'zh-CN'
  state.openAuth = vi.fn()
})

const LOCALES = ['zh-CN', 'en-US'] as const
const ITEM_KEYS = GUEST_ADVANTAGES.map((a) => a.key)

describe.each(LOCALES)('the advantages strip (%s)', (locale) => {
  beforeEach(() => {
    state.locale = locale
  })
  const bundle = () => bundles[locale] as unknown as Bundle

  it('shows all nine points as chips without opening anything', () => {
    render(<GuestAdvantages collapsed={false} />)
    const chips = screen.getAllByRole('listitem')
    expect(chips).toHaveLength(9)
    for (const key of ITEM_KEYS) {
      expect(screen.getByText(lookup(bundle(), `chat.guestAdv.items.${key}.chip`))).toBeTruthy()
    }
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('states the headline with the emphasised "AI included" part', () => {
    render(<GuestAdvantages collapsed={false} />)
    const h = screen.getByRole('heading', { level: 2 })
    expect(h.textContent).toBe(
      lookup(bundle(), 'chat.guestAdv.title') + lookup(bundle(), 'chat.guestAdv.titleEm'),
    )
    expect(h.querySelector('em')?.textContent).toBe(lookup(bundle(), 'chat.guestAdv.titleEm'))
  })

  it('folds down to the headline and the link once the guest has asked something', () => {
    render(<GuestAdvantages collapsed />)
    expect(screen.queryAllByRole('listitem')).toHaveLength(0)
    expect(screen.getByRole('heading', { level: 2 })).toBeTruthy()
    expect(
      screen.getByRole('button', { name: new RegExp(lookup(bundle(), 'chat.guestAdv.more')) }),
    ).toBeTruthy()
  })

  it('opens the drawer from the link with every full point and the sign-in button', () => {
    render(<GuestAdvantages collapsed={false} />)
    fireEvent.click(
      screen.getByRole('button', { name: new RegExp(lookup(bundle(), 'chat.guestAdv.more')) }),
    )
    const dialog = screen.getByRole('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(within(dialog).getByText(lookup(bundle(), 'chat.guestAdv.drawerTitle'))).toBeTruthy()
    for (const key of ITEM_KEYS) {
      expect(within(dialog).getByText(lookup(bundle(), `chat.guestAdv.items.${key}.title`))).toBeTruthy()
      // Compare raw text: the deploy point is four lines and getByText would collapse them.
      const bodies = Array.from(dialog.querySelectorAll('.guest-adv-list p')).map((p) => p.textContent)
      expect(bodies).toContain(lookup(bundle(), `chat.guestAdv.items.${key}.body`))
    }
  })

  it('opens the drawer from a chip too', () => {
    render(<GuestAdvantages collapsed={false} />)
    fireEvent.click(screen.getByText(lookup(bundle(), 'chat.guestAdv.items.deploy.chip')))
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('closes with the close button, with Escape and by clicking outside', () => {
    render(<GuestAdvantages collapsed={false} />)
    const open = () =>
      fireEvent.click(
        screen.getByRole('button', { name: new RegExp(lookup(bundle(), 'chat.guestAdv.more')) }),
      )
    open()
    fireEvent.click(screen.getByRole('button', { name: lookup(bundle(), 'chat.guestAdv.close') }))
    expect(screen.queryByRole('dialog')).toBeNull()
    open()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    open()
    fireEvent.click(screen.getByRole('dialog').parentElement as HTMLElement)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('does not close when the click is inside the drawer', () => {
    render(<GuestAdvantages collapsed={false} />)
    fireEvent.click(screen.getByText(lookup(bundle(), 'chat.guestAdv.items.audit.chip')))
    fireEvent.click(screen.getByRole('dialog'))
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('offers sign-in from the drawer: closes it and opens the sign-in dialog', () => {
    render(<GuestAdvantages collapsed={false} />)
    fireEvent.click(screen.getByText(lookup(bundle(), 'chat.guestAdv.items.audit.chip')))
    fireEvent.click(screen.getByRole('button', { name: lookup(bundle(), 'chat.guestAdv.cta') }))
    expect(state.openAuth).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('the wording, in both languages', () => {
  const texts = (bundle: Bundle) => {
    const adv = (bundle.chat as Bundle).guestAdv as Bundle
    return JSON.stringify(adv)
  }

  it('has every string in both languages, none empty', () => {
    const shape = (b: unknown, prefix = ''): string[] =>
      Object.entries(b as Bundle).flatMap(([k, v]) =>
        typeof v === 'string'
          ? [`${prefix}${k}:${v.trim() === '' ? 'EMPTY' : 'ok'}`]
          : shape(v, `${prefix}${k}.`),
      )
    const zh = shape(((zhCN.chat as unknown) as Bundle).guestAdv)
    const en = shape(((enUS.chat as unknown) as Bundle).guestAdv)
    expect(en).toEqual(zh)
    expect(zh.filter((s) => s.endsWith('EMPTY'))).toEqual([])
    const items = (((zhCN.chat as unknown) as Bundle).guestAdv as Bundle).items as Bundle
    expect(Object.keys(items)).toEqual(ITEM_KEYS)
  })

  it('writes English without leftover Chinese and Chinese in Chinese', () => {
    expect(/[\u4e00-\u9fff]/.test(texts(enUS as unknown as Bundle))).toBe(false)
    const items = ((zhCN.chat as unknown) as Bundle).guestAdv as Bundle
    expect(/[\u4e00-\u9fff]/.test(JSON.stringify(items))).toBe(true)
  })

  it.each([
    ['zh-CN', zhCN],
    ['en-US', enUS],
  ])('keeps the claims we cannot back out of the %s text', (_name, bundle) => {
    const all = texts(bundle as unknown as Bundle)
    // Picture search is listed as a limitation on the public page, so it is not an advantage.
    expect(all).not.toMatch(/以图搜图|picture search|image search/i)
    // "Fully offline" is not true: login codes and first model download need the network.
    expect(all).not.toMatch(/完全断网|默认不出网|全程内网|air-?gapped|fully offline/i)
    // No superlatives.
    expect(all).not.toMatch(/最好|最强|第一|唯一|业界领先|best|#1|unique|leading|unrivall?ed/i)
  })

  it('names the assistants that can connect', () => {
    const body = lookup(zhCN as unknown as Bundle, 'chat.guestAdv.items.agents.body')
    for (const name of ['ChatGPT', 'Claude', 'Cursor', 'Codex', 'DeepSeek Harness', 'MCP']) {
      expect(body).toContain(name)
    }
    expect(lookup(zhCN as unknown as Bundle, 'chat.guestAdv.items.agents.chip')).toContain('ChatGPT')
    const en = lookup(enUS as unknown as Bundle, 'chat.guestAdv.items.agents.body')
    for (const name of ['ChatGPT', 'Claude', 'Cursor', 'Codex', 'DeepSeek Harness', 'MCP']) {
      expect(en).toContain(name)
    }
  })

  it('says outside assistants are off by default', () => {
    expect(lookup(zhCN as unknown as Bundle, 'chat.guestAdv.items.agents.body')).toContain('默认关闭')
    expect(lookup(enUS as unknown as Bundle, 'chat.guestAdv.items.agents.body')).toContain('off by default')
  })

  it('says, as the ninth and last point, that personal and enterprise knowledge bases are both supported', () => {
    expect(ITEM_KEYS).toHaveLength(9)
    expect(ITEM_KEYS[ITEM_KEYS.length - 1]).toBe('editions')
    const zh = lookup(zhCN as unknown as Bundle, 'chat.guestAdv.items.editions.body')
    expect(zh).toContain('个人知识库')
    expect(zh).toContain('企业')
    expect(lookup(zhCN as unknown as Bundle, 'chat.guestAdv.items.editions.chip')).toContain('个人')
    const en = lookup(enUS as unknown as Bundle, 'chat.guestAdv.items.editions.body')
    expect(en).toContain('personal knowledge base')
    expect(lookup(enUS as unknown as Bundle, 'chat.guestAdv.items.editions.chip')).toMatch(/Personal and enterprise/)
    expect(en).toContain('enterprise')
  })

  it('opens the drawer lead on both kinds of knowledge base, and the link counts nine', () => {
    expect(lookup(zhCN as unknown as Bundle, 'chat.guestAdv.drawerLead')).toContain('个人和企业都能用')
    expect(lookup(zhCN as unknown as Bundle, 'chat.guestAdv.more')).toContain('9')
    expect(lookup(enUS as unknown as Bundle, 'chat.guestAdv.drawerLead')).toMatch(/individuals and companies/)
    expect(lookup(enUS as unknown as Bundle, 'chat.guestAdv.more')).toContain('9')
  })

  it('does not call out jargon such as per-document permission wording', () => {
    expect(texts(zhCN as unknown as Bundle)).not.toContain('单文档权限')
  })
})

describe('the starter questions', () => {
  it('have a label in both languages for every key, and the keys fit the server limit', () => {
    for (const bundle of [zhCN, enUS]) {
      const labels = (((bundle.chat as unknown) as Bundle).guest as Bundle).starters as Bundle
      for (const key of GUEST_STARTER_KEYS) {
        expect(typeof labels[key], key).toBe('string')
        expect((labels[key] as string).trim().length, key).toBeGreaterThan(0)
        expect(key.length).toBeLessThanOrEqual(32)
      }
      // No label without a key: an orphan would never be offered.
      expect(Object.keys(labels).sort()).toEqual([...GUEST_STARTER_KEYS].sort())
    }
  })

  it('lead with the security questions the new banner promises', () => {
    const keys: readonly string[] = GUEST_STARTER_KEYS
    for (const k of ['visibility', 'offboard', 'connect', 'audit']) expect(keys).toContain(k)
    expect(keys.indexOf('visibility')).toBeLessThan(keys.indexOf('start'))
    expect(new Set(keys).size).toBe(keys.length)
  })
})
