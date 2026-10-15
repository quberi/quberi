const CJK = /[\u3400-\u9fff]/

/**
 * Pick one half of a bilingual "中文 / English" name by interface language.
 *
 * The seeded demo content (organisation, assistant, guest user) stores a name
 * in both languages in one string. Returns ``name`` untouched unless it is
 * exactly two parts around " / " with Chinese on one side and none on the
 * other, so ordinary names such as "R&D / Ops" or "A / B / C" are safe.
 *
 * Only use this on data a *guest* sees: a signed-in user's own names must be
 * shown exactly as they typed them.
 */
export function localizedName(name: string, locale: string): string {
  const parts = name.split(' / ')
  if (parts.length !== 2) return name
  const a = parts[0].trim()
  const b = parts[1].trim()
  const aCjk = CJK.test(a)
  if (aCjk === CJK.test(b)) return name
  const zh = aCjk ? a : b
  const en = aCjk ? b : a
  return locale.startsWith('zh') ? zh : en
}
