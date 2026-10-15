import { Languages } from 'lucide-react'
import { useTranslation } from 'react-i18next'

/**
 * 中文 / English toggle for the stand-alone tool pages (consent, result),
 * which have no app shell around them. Same behaviour and look as the one on
 * the sign-in page: the choice is remembered under `quberi.locale`.
 */
export function LanguageSwitch() {
  const { i18n } = useTranslation()
  const current = i18n.resolvedLanguage === 'zh-CN' ? 'zh-CN' : 'en-US'
  const label = current === 'en-US' ? '中文' : 'English'

  const toggle = () => {
    const next = current === 'en-US' ? 'zh-CN' : 'en-US'
    try {
      window.localStorage.setItem('quberi.locale', next)
    } catch {
      /* storage unavailable: the switch still works for this visit */
    }
    void i18n.changeLanguage(next)
  }

  return (
    <button className="signin-lang" onClick={toggle} title={label} type="button">
      <Languages size={14} />
      <span>{label}</span>
    </button>
  )
}
