// Build-time guard: the crawler-visible HTML must keep its content.
//
// Search and AI crawlers mostly do not run JavaScript, so what matters is the
// HTML the server sends, not what React renders. This fails the build if the
// built index.html loses its static Chinese intro, or if sitemap/robots break.
import { existsSync, readFileSync, readdirSync } from 'node:fs'
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

// Two language versions on separate URLs, each pointing at the other.
const alternates = (page) =>
  ['hreflang="zh-CN" href="https://ai.quberi.com/"', 'hreflang="en" href="https://ai.quberi.com/en"'].filter(
    (a) => !page.includes(a),
  )
for (const miss of alternates(html)) errors.push(`index.html is missing alternate ${miss}`)

const en = read('en.html')
for (const miss of alternates(en)) errors.push(`en.html is missing alternate ${miss}`)
if (!/<html lang="en">/.test(en)) errors.push('en.html is not lang="en"')
if (/[\u4e00-\u9fff]/.test(en.replace(/<a class="lang"[^>]*>[^<]*<\/a>/, ''))) {
  errors.push('en.html contains Chinese text outside the language link (one language per page)')
}
const enText = en
  .replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/gi, '')
  .replace(/<[^>]+>/g, ' ')
  .replace(/\s+/g, ' ')
  .trim()
if (enText.length < 800) errors.push(`en.html visible text is ${enText.length} chars, need >= 800`)
if (!/per document, not per space/i.test(enText)) errors.push('en.html lacks the per-document definition')

// "Why a knowledge base" pages: real content in each language, one language per page, and the
// parts that keep them honest (the limits, and the two kinds of knowledge base) must stay.
const visible = (page) =>
  page
    .replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/gi, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
const why = read('why.html')
const whyEn = read('why-en.html')
if (!/<html lang="zh-CN">/.test(why)) errors.push('why.html is not lang="zh-CN"')
if (!/<html lang="en">/.test(whyEn)) errors.push('why-en.html is not lang="en"')
if (visible(why).length < 1500) errors.push('why.html has too little text')
if (visible(whyEn).length < 1500) errors.push('why-en.html has too little text')
for (const kw of ['个人', '企业', '政府部门', '部署与使用前可以了解']) {
  if (!why.includes(kw)) errors.push(`why.html is missing "${kw}"`)
}
for (const kw of ['Government departments', 'Good to know before you start']) {
  if (!whyEn.includes(kw)) errors.push(`why-en.html is missing "${kw}"`)
}
if (/[\u4e00-\u9fff]/.test(whyEn.replace(/<a class="lang"[^>]*>[^<]*<\/a>/, '').replace(/\(in Chinese\)/, ''))) {
  errors.push('why-en.html contains Chinese text outside the language link (one language per page)')
}
// The introduction is for the home page only. Every other address (the sign-in page a tool
// opens, a deep link) must swap it for a quiet "loading", or a slow load shows marketing
// copy where the login should be.
if (!/<script>[\s\S]*location\.pathname[\s\S]*boot-loading[\s\S]*<\/script>/.test(html)) {
  errors.push('index.html lost the script that hides the introduction away from the home page')
}
// Wording. What the public sees (the pages, the README, the app's own text) says what Quberi does and how to
// run it. It does not describe the product as unfinished or untested: facts such as network access or
// what to contact Quberi about are stated as facts. Words that cast the product as early or unproven
// fail the build, so they do not creep back in.
const UNFINISHED = [
  /早期/,
  /不具备/,
  /没有充分验证|还没有充分验证|未经充分验证/,
  /没有现成的镜像/,
  /\bearly[- ]stage\b/i,
  /\bat an early stage\b/i,
  /\brough edges\b/i,
  /no (ready-made|prebuilt) images?/i,
  /not (yet |been )?(thoroughly )?tested/i,
  /have not tested/i,
]
const publicText = []
for (const name of readdirSync(dist)) {
  if (name.endsWith('.html') && !/^(google|baidu_verify)/.test(name)) publicText.push([name, read(name)])
}
const localesDir = resolve(import.meta.dirname, '..', 'src', 'i18n', 'locales')
if (existsSync(localesDir)) {
  for (const name of readdirSync(localesDir)) publicText.push([`locales/${name}`, readFileSync(resolve(localesDir, name), 'utf8')])
}
const readme = resolve(import.meta.dirname, '..', '..', '..', 'README.md')
if (existsSync(readme)) publicText.push(['README.md', readFileSync(readme, 'utf8')])
for (const [name, body] of publicText) {
  for (const re of UNFINISHED) {
    const hit = body.match(re)
    if (hit) errors.push(`${name} says "${hit[0]}": state the fact (what it needs, who to contact) instead of calling the product unfinished`)
  }
}
const sitemap = read('sitemap.xml')
for (const path of ['/why', '/why-en']) {
  if (!sitemap.includes(`<loc>https://ai.quberi.com${path}</loc>`)) errors.push(`sitemap.xml does not list ${path}`)
}
if (!sitemap.startsWith('<?xml')) errors.push('sitemap.xml is not XML')
if (!sitemap.includes('<loc>https://ai.quberi.com/en</loc>')) errors.push('sitemap.xml does not list /en')

// Every page in the sitemap must be canonical to itself. A language version that
// points its canonical at another language ("/en" -> "/") is treated as a
// duplicate and left out of the index, so this is checked for every URL.
const ORIGIN = 'https://ai.quberi.com'
const locs = [...sitemap.matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1].trim())
if (locs.length === 0) errors.push('sitemap.xml lists no pages')
for (const loc of locs) {
  if (!loc.startsWith(ORIGIN)) {
    errors.push(`sitemap.xml lists ${loc}, which is not on ${ORIGIN}`)
    continue
  }
  const path = loc.slice(ORIGIN.length) || '/'
  const file = path === '/' ? 'index.html' : `${path.replace(/^\//, '')}.html`
  let page
  try {
    page = read(file)
  } catch {
    errors.push(`sitemap.xml lists ${loc} but dist/${file} does not exist`)
    continue
  }
  const canonicals = [...page.matchAll(/<link\s+rel="canonical"\s+href="([^"]*)"/g)].map((m) => m[1])
  if (canonicals.length !== 1) {
    errors.push(`${file} has ${canonicals.length} canonical links, need exactly 1`)
  } else if (canonicals[0] !== loc) {
    errors.push(`${file} canonical is ${canonicals[0]}, expected itself (${loc})`)
  }
  // A page that declares language alternates must list itself among them.
  const alternateHrefs = [...page.matchAll(/<link\s+rel="alternate"\s+hreflang="[^"]*"\s+href="([^"]*)"/g)].map(
    (m) => m[1],
  )
  if (alternateHrefs.length > 0 && !alternateHrefs.includes(loc)) {
    errors.push(`${file} declares hreflang alternates but not one for itself (${loc})`)
  }
}
if (!/^Sitemap:/m.test(read('robots.txt'))) errors.push('robots.txt has no Sitemap line')

if (errors.length > 0) {
  console.error('SEO check failed:\n- ' + errors.join('\n- '))
  process.exit(1)
}
console.log(`SEO check passed (${text.length} visible chars in index.html)`)
