import { describe, expect, it } from 'vitest'

import html from '../index.html?raw'

/**
 * Link previews are built by crawlers (Reddit, X, Slack ...) that read the
 * static HTML and never run the app. These tags therefore have to be in
 * index.html itself, with absolute URLs.
 */
function meta(attr: 'property' | 'name', key: string): string | null {
  const tag = new RegExp(`<meta[^>]*${attr}="${key}"[^>]*>`, 's').exec(html)?.[0]
  return tag ? (/content="([^"]*)"/s.exec(tag)?.[1] ?? null) : null
}

describe('index.html link preview tags', () => {
  it('has a descriptive title rather than the generic workspace name', () => {
    expect(/<title>([^<]*)<\/title>/.exec(html)?.[1]).toBe(
      'Quberi｜企业 AI 知识库，文档级权限，可接入 AI Agent',
    )
  })

  it('ships crawler-readable Chinese content inside #root (no JS needed)', () => {
    const root = /<div id="root">([\s\S]*?)<\/div>\s*<script/.exec(html)?.[1] ?? ''
    const text = root.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
    expect(text.length).toBeGreaterThanOrEqual(800)
    for (const kw of ['知识库', '权限', '文档', 'AI', 'Agent']) {
      expect(text, kw).toContain(kw)
    }
    expect(html).toContain('<html lang="zh-CN">')
  })

  it('has a description of a length search and social previews will show', () => {
    const d = meta('name', 'description')
    expect(d).not.toBeNull()
    expect(d!.length).toBeGreaterThan(50)
    expect(d!.length).toBeLessThanOrEqual(200)
  })

  it('declares the Open Graph basics', () => {
    for (const key of ['og:title', 'og:description', 'og:type', 'og:url', 'og:image']) {
      expect(meta('property', key), key).toBeTruthy()
    }
  })

  it('uses absolute https URLs for the page and the image (crawlers cannot resolve relative ones)', () => {
    for (const key of ['og:url', 'og:image']) {
      expect(meta('property', key), key).toMatch(/^https:\/\/ai\.quberi\.com\//)
    }
    expect(meta('name', 'twitter:image')).toMatch(/^https:\/\/ai\.quberi\.com\//)
  })

  it('declares the image size and asks for a large summary card', () => {
    expect(meta('property', 'og:image:width')).toBe('1200')
    expect(meta('property', 'og:image:height')).toBe('630')
    expect(meta('name', 'twitter:card')).toBe('summary_large_image')
  })

  it('makes no claim that cannot be backed up', () => {
    expect(html.toLowerCase()).not.toMatch(/most powerful|best|#1|number one/)
  })
})
