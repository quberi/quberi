import {
  Camera,
  Check,
  KeyRound,
  Mail,
  Pencil,
  ShieldCheck,
  Smartphone,
  Sparkles,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '../api/client'
import { ConnectedTools } from '../oauth/ConnectedTools'
import { ConnectToolsHelp } from '../oauth/ConnectToolsHelp'
import { useAuth } from '../auth/AuthContext'

import { Building2, Plus } from 'lucide-react'

import {
  createTeamOrganization,
  listMyOrganizations,
  switchOrganization,
  type MyOrganization,
} from '../auth/api'

import {
  avatarSrc,
  feishuLinkUrl,
  getAccount,
  googleLinkUrl,
  googleUnlink,
  requestAccountEmailCode,
  requestAccountPhoneCode,
  updateDisplayName,
  uploadAvatar,
  verifyAccountEmail,
  verifyAccountPhone,
  type AccountProfile,
} from './api'

export function AccountPanel() {
  const { t } = useTranslation()
  const { credentials, updateDisplayName: patchName } = useAuth()

  const [profile, setProfile] = useState<AccountProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!credentials) return
    setLoading(true)
    setError(null)
    try {
      setProfile(await getAccount(credentials))
    } catch (err) {
      setError(err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err))
    } finally {
      setLoading(false)
    }
  }, [credentials])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="account-page">
      <h1 className="account-title">{t('account.title')}</h1>

      {loading && <div className="state-panel">{t('account.loading')}</div>}
      {error && !loading && <div className="state-panel error-state">{error}</div>}

      {!loading && profile && (
        <div className="account-card">
          <ProfileCard
            profile={profile}
            onChanged={(p) => {
              setProfile(p)
              patchName(p.display_name)
            }}
          />

          <div className="account-divider" />

          <div className="account-methods-head">
            <h2>{t('account.methods')}</h2>
            <p>{t('account.methodsHint')}</p>
          </div>
          <EmailMethod profile={profile} onChanged={setProfile} />
          {/* Phone bind is hidden — no SMS provider wired up yet, so the
              "send code" action would silently go nowhere. Kept intact
              below (PhoneMethod) so re-enabling it later is a one-line
              change, not a rewrite. */}
          <GoogleMethod profile={profile} onChanged={setProfile} onLinked={load} />
          <FeishuMethod profile={profile} onLinked={load} />

          <div className="account-divider" />

          <ConnectToolsHelp />

          <ConnectedTools />

          <OrganizationsSection />
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Organizations: list every org the identity belongs to, switch between
// them, and create a new team. Switching is always manual — creating a
// team does not change the caller's active session.
// ---------------------------------------------------------------------------

function OrganizationsSection() {
  const { t } = useTranslation()
  const { credentials, signInWithSession } = useAuth()

  const [orgs, setOrgs] = useState<MyOrganization[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [switchingId, setSwitchingId] = useState<string | null>(null)

  const [creating, setCreating] = useState(false)
  const [teamName, setTeamName] = useState('')
  const [createBusy, setCreateBusy] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const readErr = (err: unknown) =>
    err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

  const load = useCallback(async () => {
    if (!credentials) return
    setLoading(true)
    setError(null)
    try {
      const res = await listMyOrganizations(credentials)
      setOrgs(res.items)
    } catch (err) {
      setError(readErr(err))
    } finally {
      setLoading(false)
    }
  }, [credentials])

  useEffect(() => {
    void load()
  }, [load])

  const switchTo = async (organizationId: string) => {
    if (!credentials || switchingId) return
    setSwitchingId(organizationId)
    setError(null)
    try {
      const { token, user } = await switchOrganization(credentials, organizationId)
      signInWithSession(token, user)
      await load()
    } catch (err) {
      setError(readErr(err))
    } finally {
      setSwitchingId(null)
    }
  }

  const createTeam = async () => {
    if (!credentials || !teamName.trim() || createBusy) return
    setCreateBusy(true)
    setCreateError(null)
    try {
      await createTeamOrganization(credentials, teamName.trim())
      setTeamName('')
      setCreating(false)
      await load()
    } catch (err) {
      setCreateError(readErr(err))
    } finally {
      setCreateBusy(false)
    }
  }

  return (
    <div className="account-orgs">
      <div className="account-methods-head">
        <h2>{t('account.orgs.title')}</h2>
        <p>{t('account.orgs.hint')}</p>
      </div>

      {loading && <div className="state-panel">{t('account.orgs.loading')}</div>}
      {error && !loading && <div className="account-error">{error}</div>}

      {!loading && orgs && (
        <ul className="account-org-list">
          {orgs.map((o) => (
            <li className="account-org-row" key={o.organization_id}>
              <span className="account-org-icon" aria-hidden="true">
                <Building2 size={15} />
              </span>
              <span className="account-org-body">
                <strong>{o.name}</strong>
                <small>
                  {t(`account.orgs.tier.${o.tier}`)}
                  {o.is_pending ? ` · ${t('account.orgs.pending')}` : ''}
                </small>
              </span>
              {o.is_current ? (
                <span className="account-badge ok">{t('account.orgs.current')}</span>
              ) : (
                <button
                  className="ghost-button"
                  disabled={switchingId !== null}
                  onClick={() => void switchTo(o.organization_id)}
                  type="button"
                >
                  {switchingId === o.organization_id
                    ? t('account.orgs.switching')
                    : t('account.orgs.switchTo')}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {!creating && (
        <button className="ghost-button account-org-create-toggle" onClick={() => setCreating(true)} type="button">
          <Plus size={14} /> {t('account.orgs.createTeam')}
        </button>
      )}
      {creating && (
        <div className="account-inline-form account-org-create-form">
          <input
            autoFocus
            maxLength={200}
            onChange={(e) => setTeamName(e.target.value)}
            placeholder={t('account.orgs.teamNamePlaceholder')}
            type="text"
            value={teamName}
          />
          <button
            className="primary-button"
            disabled={!teamName.trim() || createBusy}
            onClick={() => void createTeam()}
            type="button"
          >
            <Building2 size={15} /> {t('account.orgs.create')}
          </button>
          <button
            className="link-button"
            onClick={() => {
              setCreating(false)
              setTeamName('')
              setCreateError(null)
            }}
            type="button"
          >
            {t('account.orgs.cancel')}
          </button>
        </div>
      )}
      {createError && <div className="account-error">{createError}</div>}
      {!creating && <p className="account-org-create-hint">{t('account.orgs.createHint')}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Profile: avatar (upload) + display name (inline edit)
// ---------------------------------------------------------------------------

function ProfileCard({
  profile,
  onChanged,
}: {
  profile: AccountProfile
  onChanged: (p: AccountProfile) => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const fileRef = useRef<HTMLInputElement>(null)

  const [editingName, setEditingName] = useState(false)
  const [name, setName] = useState(profile.display_name)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const readErr = (err: unknown) =>
    err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

  const src = avatarSrc(profile.avatar_url)
  const initial = (profile.display_name?.[0] ?? '?').toUpperCase()

  const subtitle = profile.has_email_login
    ? profile.email
    : profile.feishu_account ?? t('account.noEmail')

  const pickAvatar = () => fileRef.current?.click()

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file || !credentials) return
    setBusy(true)
    setError(null)
    try {
      onChanged(await uploadAvatar(credentials, file))
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  const saveName = async () => {
    if (!credentials || !name.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      onChanged(await updateDisplayName(credentials, name.trim()))
      setEditingName(false)
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="account-profile">
      <button
        className="account-avatar-btn"
        onClick={pickAvatar}
        title={t('account.avatar.edit')}
        type="button"
        disabled={busy}
      >
        {src ? (
          <img className="account-avatar-img" src={src} alt="" />
        ) : (
          <span className="account-avatar" aria-hidden="true">
            {initial}
          </span>
        )}
        <span className="account-avatar-overlay">
          <Camera size={16} />
        </span>
      </button>
      <input
        ref={fileRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,image/gif"
        hidden
        onChange={onFile}
      />

      <div className="account-hero-body">
        {editingName ? (
          <div className="account-name-edit">
            <input
              autoFocus
              maxLength={120}
              onChange={(e) => setName(e.target.value)}
              value={name}
            />
            <button
              className="primary-button"
              disabled={!name.trim() || busy}
              onClick={saveName}
              type="button"
            >
              <Check size={15} /> {t('account.name.save')}
            </button>
            <button
              className="link-button"
              onClick={() => {
                setEditingName(false)
                setName(profile.display_name)
                setError(null)
              }}
              type="button"
            >
              {t('account.name.cancel')}
            </button>
          </div>
        ) : (
          <h1 className="account-name">
            {profile.display_name}
            <button
              className="account-name-edit-btn"
              onClick={() => setEditingName(true)}
              title={t('account.name.edit')}
              type="button"
            >
              <Pencil size={14} />
            </button>
          </h1>
        )}
        <p>{subtitle}</p>
        {error && <div className="account-error">{error}</div>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Email method row
// ---------------------------------------------------------------------------

function EmailMethod({
  profile,
  onChanged,
}: {
  profile: AccountProfile
  onChanged: (p: AccountProfile) => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()

  const [editing, setEditing] = useState(false)
  const [step, setStep] = useState<'email' | 'code'>('email')
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const readErr = (err: unknown) =>
    err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

  const reset = () => {
    setEditing(false)
    setStep('email')
    setEmail('')
    setCode('')
    setError(null)
    setNotice(null)
  }

  const sendCode = async () => {
    if (!credentials || !email.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await requestAccountEmailCode(credentials, email.trim().toLowerCase())
      setNotice(res.message)
      setStep('code')
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  const verify = async () => {
    if (!credentials || code.trim().length < 4 || busy) return
    setBusy(true)
    setError(null)
    try {
      onChanged(await verifyAccountEmail(credentials, email.trim().toLowerCase(), code.trim()))
      reset()
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="account-method">
      <div className="account-method-row">
        <span className="account-method-icon">
          <Mail size={17} />
        </span>
        <div className="account-method-body">
          <strong>{t('account.email.title')}</strong>
          <small>{profile.has_email_login ? profile.email : t('account.email.none')}</small>
        </div>
        <div className="account-method-action">
          {profile.has_email_login && profile.email_verified && (
            <span className="account-badge ok">
              <ShieldCheck size={12} /> {t('account.email.verified')}
            </span>
          )}
          {!editing && (
            <button className="ghost-button" onClick={() => setEditing(true)} type="button">
              {profile.has_email_login ? t('account.email.changeLabel') : t('account.email.setLabel')}
            </button>
          )}
        </div>
      </div>

      {editing && (
        <div className="account-method-form">
          {step === 'email' && (
            <div className="account-inline-form">
              <input
                autoFocus
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                type="email"
                value={email}
              />
              <button
                className="primary-button"
                disabled={!email.trim() || busy}
                onClick={sendCode}
                type="button"
              >
                <Mail size={15} /> {t('account.email.sendCode')}
              </button>
              <button className="link-button" onClick={reset} type="button">
                {t('account.email.back')}
              </button>
            </div>
          )}
          {step === 'code' && (
            <div className="account-inline-form">
              {notice && <div className="account-notice">{notice}</div>}
              <input
                autoFocus
                inputMode="numeric"
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                placeholder={t('account.email.codeShort')}
                value={code}
              />
              <button
                className="primary-button"
                disabled={code.trim().length < 4 || busy}
                onClick={verify}
                type="button"
              >
                <KeyRound size={15} /> {t('account.email.verify')}
              </button>
              <button className="link-button" onClick={reset} type="button">
                {t('account.email.back')}
              </button>
            </div>
          )}
          {error && <div className="account-error">{error}</div>}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Phone method row (bind a second contact; blocks on collision)
// ---------------------------------------------------------------------------

function PhoneMethod({
  profile,
  onChanged,
}: {
  profile: AccountProfile
  onChanged: (p: AccountProfile) => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()

  const [editing, setEditing] = useState(false)
  const [step, setStep] = useState<'phone' | 'code'>('phone')
  const [phone, setPhone] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const readErr = (err: unknown) =>
    err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

  const reset = () => {
    setEditing(false)
    setStep('phone')
    setPhone('')
    setCode('')
    setError(null)
    setNotice(null)
  }

  const sendCode = async () => {
    if (!credentials || !phone.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await requestAccountPhoneCode(credentials, phone.trim())
      setNotice(res.message)
      setStep('code')
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  const verify = async () => {
    if (!credentials || code.trim().length < 4 || busy) return
    setBusy(true)
    setError(null)
    try {
      onChanged(await verifyAccountPhone(credentials, phone.trim(), code.trim()))
      reset()
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="account-method">
      <div className="account-method-row">
        <span className="account-method-icon">
          <Smartphone size={17} />
        </span>
        <div className="account-method-body">
          <strong>{t('account.phone.title')}</strong>
          <small>{profile.has_phone ? profile.phone : t('account.phone.none')}</small>
        </div>
        <div className="account-method-action">
          {profile.has_phone && (
            <span className="account-badge ok">
              <ShieldCheck size={12} /> {t('account.phone.verified')}
            </span>
          )}
          {!editing && (
            <button className="ghost-button" onClick={() => setEditing(true)} type="button">
              {profile.has_phone ? t('account.phone.changeLabel') : t('account.phone.setLabel')}
            </button>
          )}
        </div>
      </div>

      {editing && (
        <div className="account-method-form">
          {step === 'phone' && (
            <div className="account-inline-form">
              <input
                autoFocus
                inputMode="tel"
                onChange={(e) => setPhone(e.target.value)}
                placeholder={t('account.phone.placeholder')}
                type="tel"
                value={phone}
              />
              <button
                className="primary-button"
                disabled={!phone.trim() || busy}
                onClick={sendCode}
                type="button"
              >
                <Smartphone size={15} /> {t('account.phone.sendCode')}
              </button>
              <button className="link-button" onClick={reset} type="button">
                {t('account.phone.back')}
              </button>
            </div>
          )}
          {step === 'code' && (
            <div className="account-inline-form">
              {notice && <div className="account-notice">{notice}</div>}
              <input
                autoFocus
                inputMode="numeric"
                onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                placeholder={t('account.phone.codeShort')}
                value={code}
              />
              <button
                className="primary-button"
                disabled={code.trim().length < 4 || busy}
                onClick={verify}
                type="button"
              >
                <KeyRound size={15} /> {t('account.phone.verify')}
              </button>
              <button className="link-button" onClick={reset} type="button">
                {t('account.phone.back')}
              </button>
            </div>
          )}
          {error && <div className="account-error">{error}</div>}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Google method row (link via popup consent; unlink is a direct call —
// Google's verified email means there's no bind-ticket step like Feishu)
// ---------------------------------------------------------------------------

function GoogleMethod({
  profile,
  onChanged,
  onLinked,
}: {
  profile: AccountProfile
  onChanged: (p: AccountProfile) => void
  onLinked: () => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const readErr = (err: unknown) =>
    err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err)

  const linkGoogle = async () => {
    if (!credentials || busy) return
    setBusy(true)
    setError(null)
    try {
      const { authorize_url } = await googleLinkUrl(credentials)
      const popup = window.open(authorize_url, 'quberi-google-link', 'width=520,height=680')
      if (!popup) {
        setError(t('account.google.popupBlocked'))
        setBusy(false)
        return
      }
      const onMessage = (e: MessageEvent) => {
        const d = e.data as { source?: string; ok?: boolean; message?: string } | undefined
        if (!d || d.source !== 'quberi-google-login') return
        window.removeEventListener('message', onMessage)
        setBusy(false)
        if (d.ok) onLinked()
        else setError(d.message || t('account.google.failed'))
      }
      window.addEventListener('message', onMessage)
    } catch (err) {
      setError(readErr(err))
      setBusy(false)
    }
  }

  const unlinkGoogle = async () => {
    if (!credentials || busy) return
    setBusy(true)
    setError(null)
    try {
      onChanged(await googleUnlink(credentials))
    } catch (err) {
      setError(readErr(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="account-method">
      <div className="account-method-row">
        <span className="account-method-icon google">
          <Sparkles size={17} />
        </span>
        <div className="account-method-body">
          <strong>{t('account.google.title')}</strong>
          <small>
            {profile.google_linked
              ? profile.google_account ?? t('account.google.linked')
              : t('account.google.description')}
          </small>
        </div>
        <div className="account-method-action">
          {profile.google_linked ? (
            <button className="ghost-button" disabled={busy} onClick={unlinkGoogle} type="button">
              {t('account.google.unlink')}
            </button>
          ) : (
            <button className="ghost-button" disabled={busy} onClick={linkGoogle} type="button">
              {t('account.google.link')}
            </button>
          )}
        </div>
      </div>
      {error && <div className="account-error">{error}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Feishu method row
// ---------------------------------------------------------------------------

function FeishuMethod({
  profile,
  onLinked,
}: {
  profile: AccountProfile
  onLinked: () => void
}) {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const linkFeishu = async () => {
    if (!credentials || busy) return
    setBusy(true)
    setError(null)
    try {
      const { authorize_url } = await feishuLinkUrl(credentials)
      const popup = window.open(authorize_url, 'quberi-feishu-link', 'width=520,height=680')
      if (!popup) {
        setError(t('account.feishu.popupBlocked'))
        setBusy(false)
        return
      }
      const onMessage = (e: MessageEvent) => {
        const d = e.data as { source?: string; ok?: boolean; message?: string } | undefined
        if (!d || d.source !== 'quberi-feishu-login') return
        window.removeEventListener('message', onMessage)
        setBusy(false)
        if (d.ok) onLinked()
        else setError(d.message || t('account.feishu.failed'))
      }
      window.addEventListener('message', onMessage)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail || `HTTP ${err.status}` : String(err))
      setBusy(false)
    }
  }

  return (
    <div className="account-method">
      <div className="account-method-row">
        <span className="account-method-icon feishu">
          <Sparkles size={17} />
        </span>
        <div className="account-method-body">
          <strong>{t('account.feishu.title')}</strong>
          <small>
            {profile.feishu_linked
              ? profile.feishu_account ?? t('account.feishu.linked')
              : t('account.feishu.description')}
          </small>
        </div>
        <div className="account-method-action">
          {profile.feishu_linked ? (
            <span className="account-badge ok">
              <Check size={12} /> {t('account.feishu.linked')}
            </span>
          ) : (
            <button className="ghost-button" disabled={busy} onClick={linkFeishu} type="button">
              {t('account.feishu.link')}
            </button>
          )}
        </div>
      </div>
      {error && <div className="account-error">{error}</div>}
    </div>
  )
}
