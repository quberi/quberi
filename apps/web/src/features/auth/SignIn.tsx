import { ArrowLeft, KeyRound, Languages, Mail } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'

import { FeishuIcon, GoogleIcon } from './BrandIcons'
import { rememberReturnPath } from './googleRedirect'
import {
  feishuAuthorizeUrl,
  feishuBindRequestCode,
  feishuBindVerify,
  googleAuthorizeUrl,
  requestLoginCode,
  verifyLoginCode,
} from './api'
import { useAuth } from './AuthContext'
import type { SessionUser } from './AuthContext'

type Step = 'entry' | 'code'

interface Props {
  embedded?: boolean
  onAuthenticated?: () => void
  /** Shown on open — e.g. Google sent the user back with an error. */
  initialError?: string | null
}

/**
 * Unified sign-in: one email field, no separate "sign up" mode and no
 * personal/team choice. The backend decides at verify-time whether this
 * email logs into an existing account or gets a fresh personal one
 * (same convention Google/Feishu/phone already use) — see
 * ``verification.verify_code``. Creating a *team* organisation happens
 * later, from the account settings page, once the person is signed in.
 */
export function SignIn({ embedded = false, onAuthenticated, initialError = null }: Props) {
  const { t, i18n } = useTranslation()
  const { signInWithSession } = useAuth()

  const [step, setStep] = useState<Step>('entry')
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(initialError)
  const [notice, setNotice] = useState<string | null>(null)

  // Coming back to this page from Google via the browser's Back button can
  // restore it from the back/forward cache with ``busy`` still on, leaving
  // every button disabled. Re-enable them whenever the page is shown again.
  useEffect(() => {
    const onPageShow = (e: PageTransitionEvent) => {
      if (e.persisted) setBusy(false)
    }
    window.addEventListener('pageshow', onPageShow)
    return () => window.removeEventListener('pageshow', onPageShow)
  }, [])
  // Non-null once social consent succeeds for a *new* Feishu user: they
  // must bind a verified email before an account is created/linked.
  // (Google skips this — its email is already platform-verified.)
  const [bindTicket, setBindTicket] = useState<string | null>(null)

  const currentLocale = i18n.resolvedLanguage === 'zh-CN' ? 'zh-CN' : 'en-US'
  const privacyHref = currentLocale === 'zh-CN' ? '/privacy' : '/privacy-en'
  const termsHref = currentLocale === 'zh-CN' ? '/terms' : '/terms-en'
  const switchLocale = () => {
    const next = currentLocale === 'en-US' ? 'zh-CN' : 'en-US'
    window.localStorage.setItem('quberi.locale', next)
    void i18n.changeLanguage(next)
  }

  const normalizedEmail = () => email.trim().toLowerCase()
  const readErr = (err: unknown) =>
    err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

  const backToEntry = () => {
    setStep('entry')
    setCode('')
    setError(null)
  }

  // -- email (login-or-signup in one) --------------------------------------

  const sendEmailCode = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      await requestLoginCode(normalizedEmail())
      setNotice(t('signIn.notices.emailSent'))
      setStep('code')
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  const submitEmail = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || code.trim().length < 4) return
    setBusy(true)
    setError(null)
    try {
      const { token, user } = await verifyLoginCode(normalizedEmail(), code.trim())
      signInWithSession(token, user)
      onAuthenticated?.()
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  // -- Feishu email bind (new-user consent only) ---------------------------

  const sendBindCode = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!bindTicket || !email.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      await feishuBindRequestCode(bindTicket, normalizedEmail())
      setNotice(t('signIn.notices.codeSent'))
      setStep('code')
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  const verifyBind = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!bindTicket || busy || code.trim().length < 4) return
    setBusy(true)
    setError(null)
    try {
      const { token, user } = await feishuBindVerify(bindTicket, normalizedEmail(), code.trim())
      signInWithSession(token, user)
      onAuthenticated?.()
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  const cancelBind = () => {
    setBindTicket(null)
    setStep('entry')
    setCode('')
    setError(null)
    setNotice(null)
  }

  // -- Feishu quick sign-in -------------------------------------------------

  const signInWithFeishu = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const { authorize_url } = await feishuAuthorizeUrl()
      const popup = window.open(authorize_url, 'quberi-feishu-login', 'width=520,height=680')
      if (!popup) {
        setError(t('signIn.feishu.popupBlocked'))
        setBusy(false)
        return
      }
      const onMessage = (e: MessageEvent) => {
        const d = e.data as
          | {
              source?: string
              ok?: boolean
              token?: string
              user?: SessionUser
              message?: string
              pending?: string
              ticket?: string
              feishu_email?: string
            }
          | undefined
        if (!d || d.source !== 'quberi-feishu-login') return
        window.removeEventListener('message', onMessage)
        setBusy(false)
        if (d.ok && d.pending === 'bind_email' && d.ticket) {
          // New Feishu user — no account yet. Switch to the bind step.
          setBindTicket(d.ticket)
          setEmail(d.feishu_email || '')
          setCode('')
          setStep('entry')
          setNotice(null)
          setError(null)
          return
        }
        if (d.ok && d.token && d.user) {
          signInWithSession(d.token, d.user)
          onAuthenticated?.()
        } else {
          setError(d.message || t('signIn.feishu.failed'))
        }
      }
      window.addEventListener('message', onMessage)
    } catch (err) {
      setError(readErr(err))
      setBusy(false)
    }
  }

  // -- Google quick sign-in -------------------------------------------------

  const signInWithGoogle = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      // Full-page flow: this tab goes to Google and the API sends it back to
      // the app already signed in (see ``googleRedirect.ts`` /
      // ``AuthProvider``). There is nothing to wait for here, and no popup
      // for a blocker to stop. ``busy`` stays on while the browser navigates.
      const { authorize_url } = await googleAuthorizeUrl('redirect')
      rememberReturnPath()
      window.location.assign(authorize_url)
    } catch (err) {
      setError(readErr(err))
      setBusy(false)
    }
  }

  const inBind = bindTicket !== null
  const ns = inBind ? 'signIn.feishuBind' : 'signIn.email'

  // ---- Feishu email-bind card --------------------------------------------

  const bindView = (
    <>
      {step === 'entry' && (
        <form onSubmit={sendBindCode} className="signin-form">
          <label>
            <span>{t('signIn.feishuBind.emailLabel')}</span>
            <input
              autoFocus
              autoComplete="email"
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
              type="email"
              value={email}
            />
          </label>
          {error && <div className="signin-error">{error}</div>}
          <button className="primary-button" disabled={!email.trim() || busy} type="submit">
            <Mail size={16} />
            <span>{t('signIn.feishuBind.sendCode')}</span>
          </button>
          <button className="link-button" onClick={cancelBind} type="button">
            <ArrowLeft size={13} /> {t('signIn.feishuBind.cancel')}
          </button>
        </form>
      )}
      {step === 'code' && (
        <form onSubmit={verifyBind} className="signin-form">
          {notice && <div className="signin-notice">{notice}</div>}
          <label>
            <span>{t('signIn.feishuBind.codeLabel', { email })}</span>
            <input
              autoFocus
              inputMode="numeric"
              autoComplete="one-time-code"
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              placeholder="123456"
              value={code}
            />
          </label>
          {error && <div className="signin-error">{error}</div>}
          <button className="primary-button" disabled={code.trim().length < 4 || busy} type="submit">
            <KeyRound size={16} />
            <span>{t('signIn.feishuBind.verify')}</span>
          </button>
          <button className="link-button" onClick={backToEntry} type="button">
            <ArrowLeft size={13} /> {t('signIn.feishuBind.changeEmail')}
          </button>
        </form>
      )}
    </>
  )

  // ---- Email login-or-signup card -----------------------------------------

  const emailBody = (
    <>
      {step === 'entry' && (
        <form onSubmit={sendEmailCode} className="signin-form">
          <label>
            <span className="sr-only">{t(`${ns}.emailLabel`)}</span>
            <input
              aria-label={t(`${ns}.emailLabel`)}
              autoFocus
              autoComplete="email"
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
              type="email"
              value={email}
            />
          </label>
          {error && <div className="signin-error">{error}</div>}
          <button className="primary-button" disabled={!email.trim() || busy} type="submit">
            <Mail size={16} />
            <span>{t(`${ns}.sendCode`)}</span>
          </button>
        </form>
      )}
      {step === 'code' && (
        <form onSubmit={submitEmail} className="signin-form">
          {notice && <div className="signin-notice">{notice}</div>}
          <label>
            <span>{t(`${ns}.codeLabel`, { email })}</span>
            <input
              autoFocus
              inputMode="numeric"
              autoComplete="one-time-code"
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              placeholder="123456"
              value={code}
            />
          </label>
          {error && <div className="signin-error">{error}</div>}
          <button className="primary-button" disabled={code.trim().length < 4 || busy} type="submit">
            <KeyRound size={16} />
            <span>{t('signIn.email.verify')}</span>
          </button>
          <button className="link-button" onClick={backToEntry} type="button">
            <ArrowLeft size={13} /> {t('signIn.email.changeEmail')}
          </button>
        </form>
      )}
    </>
  )

  const card = (
    <div className="signin-card">
      <div className="signin-header">
        <span className="brand-mark large">
          <img src="/logo.svg" alt="Quberi" />
        </span>
        <div>
          <h1>{t(`${ns}.title`)}</h1>
          <p>{t(`${ns}.description`)}</p>
        </div>
      </div>

      {inBind ? (
        bindView
      ) : (
        <>
          {emailBody}

          <div className="signin-quick">
            <div className="signin-divider">
              <span>{t('signIn.quickLogin')}</span>
            </div>
            <div className="signin-sso-row">
              <button
                className="signin-sso-icon-button"
                disabled={busy}
                onClick={signInWithGoogle}
                title={t('signIn.google.button')}
                type="button"
              >
                <GoogleIcon size={18} />
                <span>{t('signIn.google.short')}</span>
              </button>
              <button
                className="signin-sso-icon-button"
                disabled={busy}
                onClick={signInWithFeishu}
                title={t('signIn.feishu.button')}
                type="button"
              >
                <FeishuIcon size={18} />
                <span>{t('signIn.feishu.short')}</span>
              </button>
            </div>
          </div>
        </>
      )}

      <div className="signin-footer">
        <span className="signin-legal">
          {t('signIn.legal.prefix')}
          <a href={privacyHref} target="_blank" rel="noreferrer">
            {t('signIn.legal.privacy')}
          </a>
          {t('signIn.legal.and')}
          <a href={termsHref} target="_blank" rel="noreferrer">
            {t('signIn.legal.terms')}
          </a>
        </span>
      </div>
    </div>
  )

  if (embedded) return card

  return (
    <div className="signin-shell">
      <div className="ambient ambient-one" aria-hidden="true" />
      <div className="ambient ambient-two" aria-hidden="true" />
      <button
        className="signin-lang"
        onClick={switchLocale}
        type="button"
        title={currentLocale === 'en-US' ? '中文' : 'English'}
      >
        <Languages size={14} />
        <span>{currentLocale === 'en-US' ? '中文' : 'English'}</span>
      </button>
      {card}
    </div>
  )
}
