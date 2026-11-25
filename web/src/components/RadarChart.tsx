// Radar n-giác theo số trục (bench-standard §6: 8 trục; n-gon theo số trục nhận
// được, không hard-code 8). Vẽ chồng tối đa 2 lưới để so hai kiến trúc / hai lần
// chạy. Trục chưa có điểm (score null) là KHOẢNG TRỐNG, không vẽ về 0: đa giác
// nối thẳng qua các trục có điểm, và trục trống chỉ có dấu chấm ở tâm.
// Màu dùng currentColor / Mantine vars nên sáng/tối đều đọc được; hai series
// dùng teal và blue (đọc được ở cả hai theme).
export interface RadarSeries {
  label: string
  /** Điểm từng trục, cùng thứ tự với `axes`; null = chưa đo trục đó. */
  values: (number | null)[]
}

export interface RadarChartProps {
  axes: string[]
  series: RadarSeries[]
  size?: number
}

const SERIES_COLORS = ['#12B886', '#339AF0']

export default function RadarChart({ axes, series, size = 320 }: RadarChartProps) {
  const n = axes.length
  const cx = size / 2
  const cy = size / 2
  const R = size / 2 - 44
  if (n < 3) return null

  const angle = (i: number) => (Math.PI * 2 * i) / n - Math.PI / 2
  const point = (i: number, r: number): [number, number] => [cx + r * Math.cos(angle(i)), cy + r * Math.sin(angle(i))]

  // Lưới nền: đa giác ở 25/50/75/100% + tia trục.
  const grid = [0.25, 0.5, 0.75, 1].map((f) =>
    axes
      .map((_, i) => point(i, R * f).join(','))
      .join(' '),
  )

  // Đa giác một series: nối các trục CÓ điểm theo thứ tự vòng tròn; trục trống
  // không tham gia (không vẽ về tâm = 0).
  const seriesPath = (values: (number | null)[]): string | null => {
    const pts = axes
      .map((_, i) => (values[i] == null ? null : point(i, (R * Math.max(0, Math.min(100, values[i]!))) / 100)))
      .filter((p): p is [number, number] => p !== null)
    if (pts.length < 2) return null
    return `M${pts.map((p) => p.join(',')).join(' L')} Z`
  }

  return (
    <div style={{ color: 'var(--mantine-color-text)' }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Radar chart">
        {grid.map((pts, gi) => (
          <polygon key={gi} points={pts} fill="none" stroke="currentColor" strokeOpacity={0.18} />
        ))}
        {axes.map((_, i) => {
          const [x, y] = point(i, R)
          return <line key={i} x1={cx} y1={cy} x2={x} y2={y} stroke="currentColor" strokeOpacity={0.18} />
        })}
        {series.map((s, si) => {
          const d = seriesPath(s.values)
          if (!d) return null
          const color = SERIES_COLORS[si % SERIES_COLORS.length]
          return (
            <g key={si}>
              <path d={d} fill={color} fillOpacity={0.15} stroke={color} strokeWidth={2} />
              {axes.map((_, i) => {
                const score = s.values[i]
                if (score == null) {
                  return <circle key={i} cx={cx} cy={cy} r={2.5} fill="currentColor" fillOpacity={0.4} />
                }
                const [x, y] = point(i, (R * Math.max(0, Math.min(100, score))) / 100)
                return <circle key={i} cx={x} cy={y} r={3} fill={color} />
              })}
            </g>
          )
        })}
        {axes.map((axis, i) => {
          const [x, y] = point(i, R + 26)
          return (
            <text key={i} x={x} y={y} textAnchor="middle" dominantBaseline="middle" fontSize={12} fill="currentColor">
              {axis}
            </text>
          )
        })}
      </svg>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', justifyContent: 'center', marginTop: 4 }}>
        {series.map((s, si) => (
          <span key={si} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
            <span
              style={{
                width: 12,
                height: 12,
                background: SERIES_COLORS[si % SERIES_COLORS.length],
                borderRadius: 2,
                display: 'inline-block',
              }}
            />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  )
}
