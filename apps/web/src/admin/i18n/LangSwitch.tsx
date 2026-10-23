import { type Lang, useLang } from './index'

const OPTIONS: { lang: Lang; label: string; htmlLang: string }[] = [
  { lang: 'en', label: 'EN', htmlLang: 'en' },
  { lang: 'zh', label: '中文', htmlLang: 'zh-CN' },
]

/** Two-button language control; both options stay visible, each with its own accessible name. */
export function LangSwitch() {
  const { lang, setLang, t } = useLang()
  return (
    <div className="admin-lang-switch" role="group" aria-label={t('lang.label')}>
      {OPTIONS.map((option) => (
        <button
          key={option.lang}
          aria-label={t(option.lang === 'en' ? 'lang.en' : 'lang.zh')}
          aria-pressed={lang === option.lang}
          className={lang === option.lang ? 'active' : undefined}
          lang={option.htmlLang}
          onClick={() => setLang(option.lang)}
          type="button"
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
