import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import html from '../index.html?raw'

/**
 * `index.html` carries a marketing introduction inside #root for crawlers, which fades in after
 * 1.2s if the app has not replaced it. That is right for the home page and wrong everywhere else:
 * a person sent to the sign-in page by a connected tool saw marketing copy instead of the login on a
 * slow load. A small inline script swaps it for a quiet "loading" away from the home page. These
 * tests run that very script, read out of the file, so the file and the behaviour cannot drift.
 */
function inlineScript(): string {
  const found = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1])
  expect(found.length, 'one inline script in index.html').toBe(1)
  return found[0]
}

function boot(path: string, language: string): HTMLElement {
  window.history.pushState({}, '', path)
  document.body.innerHTML = `<div id="root"><main class="static-intro"><h1>intro</h1></main></div>`
  // The script uses `location`, `document` and `navigator` as free names.
  new Function('location', 'document', 'navigator', inlineScript())(
    window.location,
    document,
    { language },
  )
  return document.getElementById('root') as HTMLElement
}

beforeEach(() => {
  document.body.innerHTML = ''
})
afterEach(() => {
  window.history.pushState({}, '', '/')
})

describe('the introduction in index.html', () => {
  it('is left alone on the home page', () => {
    for (const path of ['/', '/index.html']) {
      const root = boot(path, 'zh-CN')
      const intro = root.querySelector('main.static-intro') as HTMLElement
      expect(intro.style.display, path).not.toBe('none')
      expect(root.querySelector('.boot-loading'), path).toBeNull()
    }
  })

  it.each(['/oauth/authorize', '/oauth/connected', '/some/deep/link'])(
    'is hidden away from the home page (%s), and a quiet loading note stands in',
    (path) => {
      const root = boot(path, 'en-US')
      expect((root.querySelector('main.static-intro') as HTMLElement).style.display).toBe('none')
      const note = root.querySelector('.boot-loading') as HTMLElement
      expect(note.textContent).toBe('Loading…')
      expect(note.getAttribute('role')).toBe('status')
    },
  )

  it('says "loading" in the visitor\'s language', () => {
    expect((boot('/oauth/authorize', 'zh-CN').querySelector('.boot-loading') as HTMLElement).textContent).toBe(
      '正在加载…',
    )
    expect((boot('/oauth/authorize', 'zh').querySelector('.boot-loading') as HTMLElement).textContent).toBe(
      '正在加载…',
    )
    expect((boot('/oauth/authorize', 'fr-FR').querySelector('.boot-loading') as HTMLElement).textContent).toBe(
      'Loading…',
    )
  })

  it('does nothing, and does not fail, when there is no introduction to hide', () => {
    window.history.pushState({}, '', '/oauth/authorize')
    document.body.innerHTML = '<div id="root"></div>'
    expect(() =>
      new Function('location', 'document', 'navigator', inlineScript())(window.location, document, {
        language: 'en',
      }),
    ).not.toThrow()
    expect(document.querySelector('.boot-loading')).toBeNull()
  })

  it('keeps the text for crawlers: it is still in the HTML, just not shown away from the home page', () => {
    expect(html).toContain('<main class="static-intro">')
    expect(html).toContain('企业 AI 知识库，权限做到单份文档')
  })

  it('waits a moment before showing the loading note, so a fast load never flashes it', () => {
    expect(html).toMatch(/\.boot-loading\s*{[^}]*animation:[^;}]*400ms/)
  })
})
