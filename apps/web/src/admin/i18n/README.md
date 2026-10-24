# Platform admin console: languages

The platform admin console (`admin.html` -> `src/admin/main.tsx` -> `AdminApp.tsx`) is
available in English (`en`) and Simplified Chinese (`zh`).

This module is deliberately separate from the main app's i18next setup in `src/i18n`
and from the organisation console in `src/admin-console`. The admin console is its own
Vite entry, two languages do not need a library, and nothing under `src/admin/` may
import `i18next` or `react-i18next` (a test enforces it).

## How it works

- `index.ts` holds a tiny store read with `useSyncExternalStore`, so no provider is
  needed. The language is the stored choice (`localStorage` key `quberi.admin.lang`),
  otherwise `zh` when the browser language starts with `zh`, otherwise `en`.
- Setting the language also sets `<html lang>` (`en` / `zh-CN`) and `document.title`.
- `useLang()` returns `t`, `tCode`, `formatNumber`, `formatDateTime` and `setLang`.
  Numbers and dates follow the active locale; times that are UTC say so in the text.
- Every API request carries `Accept-Language` (`en-US` or `zh-CN`) for the active
  language.
- Messages are flat dotted keys in `en.ts` and `zh.ts`, for example
  `traffic.col.requests`. `en.ts` defines the key set; `zh.ts` is typed against it.

## Add or change a string

1. Add the key to `en.ts`, then to `zh.ts`. `pnpm lint` (`tsc --noEmit`) fails until
   both files have exactly the same keys.
2. Use it: `t('orgs.title', { n: 3 })`. In a component, take `t` from `useLang()`.
3. Parameters are written `{name}`. They are filled in one pass: a value is never
   searched for more placeholders and an unknown `{name}` stays as written, so a
   literal `{id}` needs no escaping.
4. Rich text: `<T k="clients.hint" />` (from `Rich.tsx`) renders `<code>…</code>` and
   `<strong>…</strong>` as elements. No other markup is recognised and no HTML is injected.
5. Plurals: write the English value as `{ one: '…', other: '…' }` and pass a numeric
   `n`. The Chinese value for the same key is a plain string.
6. One sentence is one key. Never build a sentence from translated fragments. When a
   word changes the sentence (hour or day), use two whole-sentence keys.
7. Keys chosen at run time must be typed, for example
   `` t(`traffic.kind.${kind}.label`) `` with `kind` narrowed to the known values, so a
   missing translation is a compile error. Values the server may send that the console
   does not know (a new kind, status or reason) are shown as sent.

Do not translate: data typed by people or sent by the API (names, client ids, redirect
addresses, host names, User-Agent strings, paths), identifiers and code samples, and
product or protocol names (MCP, OAuth, BYOK, DeepSeek and so on).

## Add a language

1. In `index.ts`, extend `Lang`, `LOCALE`, `HTML_LANG`, `DOC_TITLE` and `DICTS`.
2. Add `xx.ts` with `export const xx: Record<MessageKey, Entry> = { … }`.
3. Add a button to `LangSwitch.tsx`.
4. On the API, add the language to `Message` and `pick_language` in
   `apps/api/src/quberi_api/services/admin_messages.py`, and fill it in for every
   catalog entry.

## Messages from the API

Error responses on `/api/v1/admin/*` carry a stable code next to the English text:

```json
{ "detail": "no such tool", "code": "admin.oauth.tool_not_found" }
```

- `detail` is in the language of the request's `Accept-Language` header (English when
  the header is absent, so scripts and `curl` see the same text as before). `params`
  appears only when there are some. A param may itself be a message
  (`{ "code": "…", "params": { … } }`), which is how a sentence subject is shared.
- The console prefers its own dictionary: it looks the `code` up as `msg.<code>`
  and fills in `params`. If the code is unknown it shows `detail` as sent. A 422
  validation body shows `error.validation`, a 5xx shows `error.server`, and a failed
  request shows `error.network`. `errorMessage()` in `errors.ts` is the one helper
  every view uses; keep the raw error in state and call it while rendering, so the
  text follows a language switch.
- `GET /api/v1/admin/traffic` also returns `address_note_code`. The console renders
  the note from its dictionary and falls back to `address_note`, which the API
  already localises.

To add a code:

1. Add an entry with `en` and `zh` text to `CATALOG` in
   `apps/api/src/quberi_api/services/admin_messages.py`, one entry per line. Keep the
   `{param}` names identical in both languages.
2. Raise `AdminApiException(status, "admin.x.y", **params)` (or `CodedValueError` in
   service code) instead of a bare `HTTPException`.
3. Add `msg.admin.x.y` to `en.ts` and `zh.ts` with the same `{param}` names.

Tests check that every code used in the API source exists in the catalog and the
other way round, and that the catalog and both dictionaries list the same codes.

Intentionally not translated: messages from services shared with the main app
(onboarding), the `notes` field of the model catalog (the console does not show it),
and generic HTTP errors.

## Tests

```bash
cd apps/web && pnpm lint && pnpm test && pnpm build
cd apps/api && pytest tests/test_admin_messages.py
```
