import { useMemo, useState } from 'react'

const compact = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 2 })

export default function FactSeriesChart({ series }) {
  const [hovered, setHovered] = useState(null)
  const points = useMemo(() => {
    const valid = (series?.rows || [])
      .map((row) => ({ row, value: Number(row.value) }))
      .filter((item) => Number.isFinite(item.value))
    if (!valid.length) return []
    const values = valid.map((item) => item.value)
    const low = Math.min(...values)
    const high = Math.max(...values)
    const span = high - low || Math.abs(high) || 1
    return valid.map((item, index) => ({
      ...item,
      x: valid.length === 1 ? 50 : 5 + (index / (valid.length - 1)) * 90,
      y: 86 - ((item.value - low) / span) * 70,
    }))
  }, [series])

  if (!series) return <div className="series-empty">点击任意事实行，查看同一 taxonomy / tag / unit 的所有历史版本。</div>
  if (!points.length) return <div className="series-empty">该序列没有可绘制的数值，但原始字符串仍保留在事实表中。</div>

  const active = hovered || points[points.length - 1]
  const path = points.map((point, index) => `${index ? 'L' : 'M'} ${point.x} ${point.y}`).join(' ')
  return (
    <div className="series-chart">
      <div className="series-head">
        <div><b>{series.label || series.tag}</b><span>{series.taxonomy}:{series.tag} · {series.unit}</span></div>
        <div className="series-value"><strong>{compact.format(active.value)}</strong><span>原值 {active.row.value}</span></div>
      </div>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label={`${series.tag} 历史事实`}>
        {[16, 39.3, 62.6, 86].map((y) => <line key={y} x1="5" x2="95" y1={y} y2={y} className="grid-line" />)}
        <path d={path} className="series-line" />
        {points.map((point) => (
          <circle
            key={point.row.row_id}
            cx={point.x} cy={point.y} r={hovered?.row.row_id === point.row.row_id ? 1.9 : 1.15}
            className={point.row.form.endsWith('/A') ? 'series-dot amended' : 'series-dot'}
            onMouseEnter={() => setHovered(point)} onMouseLeave={() => setHovered(null)}
          />
        ))}
      </svg>
      <div className="series-axis"><span>{points[0].row.end_date}</span><span>{active.row.form} · 报告期 {active.row.end_date} · 可用日 {active.row.available_date}</span><span>{points.at(-1).row.end_date}</span></div>
    </div>
  )
}
