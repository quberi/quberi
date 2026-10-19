import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Keyboard behaviour for a modal dialog: Escape closes it, Tab stays inside it, focus
 * moves into it when it opens and goes back to what had focus before when it closes.
 * ``canClose`` lets a dialog refuse to close while a request is under way.
 */
export function useDialogFocus(
  ref: RefObject<HTMLElement | null>,
  onClose: () => void,
  canClose = true,
): void {
  const previous = useRef<HTMLElement | null>(null)
  const close = useRef(onClose)
  const closable = useRef(canClose)
  close.current = onClose
  closable.current = canClose

  useEffect(() => {
    previous.current = document.activeElement as HTMLElement | null
    const root = ref.current
    const first = root?.querySelector<HTMLElement>(FOCUSABLE)
    if (root && !root.contains(document.activeElement)) (first ?? root).focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (closable.current) {
          event.stopPropagation()
          close.current()
        }
        return
      }
      if (event.key !== 'Tab' || !ref.current) return
      const items = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (items.length === 0) {
        event.preventDefault()
        return
      }
      const head = items[0]
      const tail = items[items.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === head || !ref.current.contains(active))) {
        event.preventDefault()
        tail.focus()
      } else if (!event.shiftKey && (active === tail || !ref.current.contains(active))) {
        event.preventDefault()
        head.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      previous.current?.focus?.()
    }
  }, [ref])
}
