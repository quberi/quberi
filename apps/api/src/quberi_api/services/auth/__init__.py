"""Authentication services: sessions, verification codes, email settings.

Phase 1 ships email verification-code login. The pieces are deliberately
decoupled so later phases slot in without rework:

* :mod:`session` — stateless bearer session tokens (Fernet-sealed).
* :mod:`email_settings` — platform SMTP config (DB, sealed password,
  env-bootstrapped).
* :mod:`email_sender` — SMTP delivery (swap-in point for a transactional
  provider or an SMS sender later).
* :mod:`verification` — channel-agnostic one-time-code flow (email now,
  phone/SMS later reuse the same code table + logic).
"""
