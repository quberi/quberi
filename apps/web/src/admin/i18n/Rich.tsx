import { Fragment, type ReactNode } from 'react'

import { type MessageKey, type Params, useLang } from './index'

const MARKUP = /(<code>[\s\S]*?<\/code>|<strong>[\s\S]*?<\/strong>)/

/**
 * Renders a message that may contain `<code>…</code>` and `<strong>…</strong>`.
 * Only those two tags are recognised; everything else is plain text (no HTML is
 * ever injected).
 */
export function T({ k, params }: { k: MessageKey; params?: Params }): ReactNode {
  const { t } = useLang()
  const parts = t(k, params).split(MARKUP)
  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith('<code>') && part.endsWith('</code>')) {
          return <code key={index}>{part.slice(6, -7)}</code>
        }
        if (part.startsWith('<strong>') && part.endsWith('</strong>')) {
          return <strong key={index}>{part.slice(8, -9)}</strong>
        }
        return <Fragment key={index}>{part}</Fragment>
      })}
    </>
  )
}
