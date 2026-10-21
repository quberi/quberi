import type { OverviewTrendPoint } from './types'

/**
 * A small bar chart of one number per day. Plain CSS bars: no chart library for 14 numbers.
 * Every bar carries its date and value as a tooltip, and the whole chart has a text summary
 * for screen readers.
 */
export function TrendBars({
  points,
  pick,
  label,
  total,
}: {
  points: OverviewTrendPoint[]
  pick: (p: OverviewTrendPoint) => number
  label: string
  total: string
}) {
  const values = points.map(pick)
  const max = Math.max(1, ...values)
  return (
    <figure className="trend-bars">
      <figcaption>
        <span>{label}</span>
        <strong>{total}</strong>
      </figcaption>
      <div className="trend-bars-plot" role="img" aria-label={`${label}: ${total}`}>
        {points.map((p, i) => (
          <span
            className={`trend-bar${values[i] === 0 ? ' trend-bar-zero' : ''}${i === points.length - 1 ? ' trend-bar-today' : ''}`}
            key={p.day}
            style={{ height: `${Math.max(4, Math.round((values[i] / max) * 100))}%` }}
            title={`${p.day}: ${values[i]}`}
          />
        ))}
      </div>
      <div className="trend-bars-axis" aria-hidden="true">
        <span>{points[0]?.day.slice(5)}</span>
        <span>{points[points.length - 1]?.day.slice(5)}</span>
      </div>
    </figure>
  )
}
