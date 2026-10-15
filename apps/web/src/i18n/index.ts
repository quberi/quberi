import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import enUS from './locales/en-US.json'
import zhCN from './locales/zh-CN.json'

const storedLocale = window.localStorage.getItem('quberi.locale')

// Default to English regardless of browser language. Respect an explicit
// user choice (persisted under `quberi.locale`) if one was made.
void i18n.use(initReactI18next).init({
  resources: {
    'en-US': { translation: enUS },
    'zh-CN': { translation: zhCN },
  },
  lng: storedLocale ?? 'en-US',
  fallbackLng: 'en-US',
  interpolation: {
    escapeValue: false,
  },
})

export default i18n
