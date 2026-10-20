// Build-time guard: the crawler-visible HTML must keep its content.
//
// Search and AI crawlers mostly do not run JavaScript, so what matters is the
// HTML the server sends, not what React renders. This fails the build if the
// built index.html loses its static Chinese intro, or if sitemap/robots break.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const dist = resolve(import.meta.dirname, '..', 'dist')
const MIN_CHARS = 800
const KEYWORDS = ['知识库', '权限', '文档', 'AI', 'Agent']

const read = (name) => readFileSync(resolve(dist, name), 'utf8')
const errors = []

const html = read('index.html')
const text = html
  .replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/gi, '')
  .replace(/<[^>]+>/g, ' ')
  .replace(/\s+/g, ' ')
  .trim()

if (text.length < MIN_CHARS) {
  errors.push(`index.html visible text is ${text.length} chars, need >= ${MIN_CHARS}`)
}
for (const kw of KEYWORDS) {
  if (!html.includes(kw)) errors.push(`index.html is missing keyword "${kw}"`)
}
if (!/<title>[^<]*[\u4e00-\u9fff][^<]*<\/title>/.test(html)) {
  errors.push('index.html <title> is not Chinese')
}
if (!/<meta\s+name="description"\s+content="[^"]*[\u4e00-\u9fff]/.test(html)) {
  errors.push('index.html meta description is not Chinese')
}
if (!html.includes('og:image')) errors.push('index.html lost its og:image tag')

if (!read('sitemap.xml').startsWith('<?xml')) errors.push('sitemap.xml is not XML')
if (!/^Sitemap:/m.test(read('robots.txt'))) errors.push('robots.txt has no Sitemap line')

if (errors.length > 0) {
  console.error('SEO check failed:\n- ' + errors.join('\n- '))
  process.exit(1)
}
console.log(`SEO check passed (${text.length} visible chars in index.html)`)
