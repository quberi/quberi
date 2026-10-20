import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import enUS from './locales/en-US.json'
import zhCN from './locales/zh-CN.json'
import { htmlLangFor, LOCALE_STORAGE_KEY, pickLocale } from './locale'

function readStoredLocale(): string | null {
  try {
    return window.localStorage.getItem(LOCALE_STORAGE_KEY)
  } catch {
    return null // storage unavailable (private mode, blocked cookies)
  }
}

// An explicit choice (persisted under `quberi.locale`) wins; otherwise follow
// the browser's language, and fall back to English.
const browserLanguages =
  navigator.languages && navigator.languages.length > 0
    ? navigator.languages
    : [navigator.language]
const locale = pickLocale(readStoredLocale(), browserLanguages)

const applyHtmlLang = (language: string) => {
  document.documentElement.lang = htmlLangFor(language)
}
applyHtmlLang(locale)
i18n.on('languageChanged', applyHtmlLang)

void i18n.use(initReactI18next).init({
  resources: {
    'en-US': { translation: enUS },
    'zh-CN': { translation: zhCN },
  },
  lng: locale,
  fallbackLng: 'en-US',
  interpolation: {
    escapeValue: false,
  },
})

export default i18n
