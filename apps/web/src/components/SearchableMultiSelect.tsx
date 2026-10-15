import { Check, ChevronDown, Search, X } from 'lucide-react'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

export interface ComboOption {
  id: string
  label: string
  subtitle?: string
}

export interface SearchableMultiSelectProps {
  options: ComboOption[]
  selected: string[]
  onChange: (next: string[]) => void
  placeholder: string
  searchPlaceholder: string
  noResultsLabel: string
  clearLabel: string
  disabled?: boolean
}

interface PanelRect {
  top: number
  left: number
  width: number
}

/**
 * Searchable multi-select combobox: a control that renders the current
 * selection as removable chips and opens a searchable checkbox list on
 * click. Generic over whatever ``ComboOption[]`` the caller passes in —
 * no API or domain knowledge baked in — so it's reusable anywhere a plain
 * HTML `<select multiple>` would otherwise be the fallback (model access
 * governance today; space access / invite pickers are natural next users).
 *
 * The open panel is rendered through a portal into ``document.body`` and
 * positioned with ``position: fixed`` from the control's live bounding
 * rect, rather than living in normal flow next to the control. A handful
 * of these sit stacked in the same form (members / groups / org units /
 * positions / excluded members); if the panel stayed in-flow, it would
 * get painted *underneath* whatever comes after it in the DOM — later
 * fields, or even an entirely separate card further down the page — no
 * matter how high a z-index its own wrapper claims, because that only
 * wins within its own stacking context. Escaping to body sidesteps the
 * whole problem instead of chasing z-index across every container.
 *
 * Deliberately uncontrolled-free: all state (selection) lives with the
 * caller via ``selected`` + ``onChange``; this component only owns its own
 * transient UI state (open/closed, search text, computed panel position).
 */
export function SearchableMultiSelect({
  options,
  selected,
  onChange,
  placeholder,
  searchPlaceholder,
  noResultsLabel,
  clearLabel,
  disabled = false,
}: SearchableMultiSelectProps) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [rect, setRect] = useState<PanelRect | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)

  const byId = useMemo(() => {
    const m = new Map<string, ComboOption>()
    for (const o of options) m.set(o.id, o)
    return m
  }, [options])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return options
    return options.filter(
      (o) =>
        o.label.toLowerCase().includes(q) ||
        (o.subtitle ?? '').toLowerCase().includes(q),
    )
  }, [options, query])

  const recomputeRect = () => {
    const el = containerRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    setRect({ top: r.bottom + 6, left: r.left, width: r.width })
  }

  // Position the portal panel against the live control on open, and keep
  // it glued to it on scroll/resize anywhere in the document (capture
  // phase so scrolling inside a nested panel still re-triggers it).
  useLayoutEffect(() => {
    if (!open) return
    recomputeRect()
    const onReflow = () => recomputeRect()
    window.addEventListener('resize', onReflow)
    window.addEventListener('scroll', onReflow, true)
    return () => {
      window.removeEventListener('resize', onReflow)
      window.removeEventListener('scroll', onReflow, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (
        containerRef.current &&
        !containerRef.current.contains(target) &&
        panelRef.current &&
        !panelRef.current.contains(target)
      ) {
        setOpen(false)
      }
    }
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onEscape)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onEscape)
    }
  }, [open])

  useEffect(() => {
    if (open) {
      setQuery('')
      // Focus after the panel mounts so the user can type immediately.
      requestAnimationFrame(() => searchRef.current?.focus())
    }
  }, [open])

  const toggle = (id: string) => {
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id])
  }

  const remove = (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    onChange(selected.filter((x) => x !== id))
  }

  return (
    <div className="msel" ref={containerRef}>
      <button
        className={`msel-control ${open ? 'is-open' : ''}`}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        type="button"
      >
        {selected.length === 0 && <span className="msel-placeholder">{placeholder}</span>}
        {selected.map((id) => {
          const opt = byId.get(id)
          if (!opt) return null
          return (
            <span className="msel-chip" key={id}>
              {opt.label}
              <button
                aria-label={opt.label}
                onClick={(e) => remove(id, e)}
                type="button"
              >
                <X size={11} />
              </button>
            </span>
          )
        })}
        <ChevronDown size={14} className="msel-chevron" />
      </button>

      {open &&
        rect &&
        createPortal(
          <div
            className="msel-panel"
            ref={panelRef}
            role="listbox"
            style={{ top: rect.top, left: rect.left, width: rect.width }}
          >
            <div className="msel-search">
              <Search size={13} />
              <input
                onChange={(e) => setQuery(e.target.value)}
                placeholder={searchPlaceholder}
                ref={searchRef}
                type="search"
                value={query}
              />
            </div>
            {selected.length > 0 && (
              <button
                className="msel-clear"
                onClick={() => onChange([])}
                type="button"
              >
                {clearLabel}
              </button>
            )}
            <div className="msel-options">
              {filtered.map((o) => {
                const checked = selected.includes(o.id)
                return (
                  <label className="msel-option" key={o.id}>
                    <input checked={checked} onChange={() => toggle(o.id)} type="checkbox" />
                    <span className="msel-option-body">
                      <strong>{o.label}</strong>
                      {o.subtitle && <small>{o.subtitle}</small>}
                    </span>
                    {checked && <Check size={14} className="msel-option-check" />}
                  </label>
                )
              })}
              {filtered.length === 0 && <p className="msel-empty">{noResultsLabel}</p>}
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}
