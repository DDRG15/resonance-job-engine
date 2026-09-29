import { useEffect, useState } from 'react'

interface DashboardStats {
  by_tier: Record<string, number>
  score_version?: string
  fuera_de_escala?: number
  by_source: { source: string; count: number }[]
  by_day: { day: string; count: number }[]
}

interface Props {
  onClose: () => void
}

const TIER_COLOR: Record<string, string> = {
  'Tier 1': 'var(--t1)', 'Tier 2': 'var(--t2)',
  'Tier 3': 'var(--t3)', 'Tier 4': 'var(--t4)',
}
const TIER_INK: Record<string, string> = {
  'Tier 1': 'var(--t1-ink)', 'Tier 2': 'var(--t2-ink)',
  'Tier 3': 'var(--t3-ink)', 'Tier 4': 'var(--t4-ink)',
}

// F19 (pedido de el usuario, 07-10): board visual de estadísticas alimentado por
// la DB (endpoint /api/registry/dashboard, 0 tokens). Charts SVG inline con la
// paleta de tiers validada CVD — sin librería de charts (Recharts queda para
// una versión futura si crece; hoy 3 gráficos simples no la justifican).
export default function StatsBoard({ onClose }: Props) {
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetch('/api/registry/dashboard')
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`)
        return r.json() as Promise<DashboardStats>
      })
      .then((d) => !cancelled && setStats(d))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      cancelled = true
    }
  }, [])

  const tierTotal = stats
    ? Object.values(stats.by_tier).reduce((a, b) => a + b, 0)
    : 0
  const maxSource = stats && stats.by_source.length > 0 ? stats.by_source[0].count : 1
  const maxDay = stats && stats.by_day.length > 0
    ? Math.max(...stats.by_day.map((d) => d.count))
    : 1

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Estadísticas del registry</h3>
        <span className="phase">desde la base de datos · 0 tokens</span>
      </div>
      <div className="actions" style={{ marginTop: 0, marginBottom: 14 }}>
        <span style={{ flex: 1 }} />
        <button className="btn mini" onClick={onClose}>Cerrar</button>
      </div>

      {error && (
        <div className="error-banner">No se pudo leer la base de datos: <code>{error}</code></div>
      )}
      {stats === null && !error && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {[0, 1, 2].map((i) => <div className="skeleton" style={{ height: 60 }} key={i} />)}
        </div>
      )}

      {stats && (
        <div className="stats-grid">
          {/* Distribución por tier — barras horizontales apiladas + leyenda */}
          <div className="stats-card">
            <div className="stats-title">
              Distribución por tier
              {stats.score_version && <span className="phase"> · escala {stats.score_version}</span>}
            </div>
            {tierTotal === 0 ? (
              <div className="empty-state"><b>Sin datos aún</b>Corré una extracción.</div>
            ) : (
              <>
                <div className="tier-bar">
                  {(['Tier 1', 'Tier 2', 'Tier 3', 'Tier 4'] as const).map((t) => {
                    const n = stats.by_tier[t] ?? 0
                    if (n === 0) return null
                    return (
                      <div
                        key={t}
                        style={{
                          width: `${(n / tierTotal) * 100}%`,
                          background: TIER_COLOR[t],
                        }}
                        title={`${t}: ${n}`}
                      />
                    )
                  })}
                </div>
                <div className="tier-legend">
                  {(['Tier 1', 'Tier 2', 'Tier 3', 'Tier 4'] as const).map((t) => (
                    <span key={t}>
                      <i style={{ background: TIER_COLOR[t] }} />
                      {t}: <b style={{ color: TIER_INK[t] }}>{stats.by_tier[t] ?? 0}</b>
                    </span>
                  ))}
                </div>
                {/* Los avisos puntuados con otra version del algoritmo no se suman:
                    sus numeros no son comparables. Se dicen, para que no parezca que
                    desaparecieron. */}
                {(stats.fuera_de_escala ?? 0) > 0 && (
                  <p className="side-note" data-testid="fuera-de-escala">
                    {stats.fuera_de_escala} avisos más, puntuados con otra versión del
                    scoring, no se cuentan acá.
                  </p>
                )}
              </>
            )}
          </div>

          {/* Volumen por día (14d) — sparkline de barras */}
          <div className="stats-card">
            <div className="stats-title">Jobs capturados — últimos 14 días</div>
            {stats.by_day.length === 0 ? (
              <div className="empty-state"><b>Sin actividad reciente</b></div>
            ) : (
              <div className="day-chart">
                {stats.by_day.map((d) => (
                  <div className="day-col" key={d.day} title={`${d.day}: ${d.count}`}>
                    {/* riel con altura propia: la barra mide % SOLO del riel,
                        nunca del label — así no se montan (fix pulido 07-17) */}
                    <div className="day-track">
                      <div
                        className="day-fill"
                        style={{ height: `${Math.max(4, (d.count / maxDay) * 100)}%` }}
                      />
                    </div>
                    <span className="day-lbl">{d.day.slice(8)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Por fuente — top boards */}
          <div className="stats-card stats-wide">
            <div className="stats-title">Por fuente (top boards)</div>
            {stats.by_source.map((s) => (
              <div className="source-row" key={s.source}>
                <span className="source-name">{s.source}</span>
                <div className="source-bar">
                  <i style={{ width: `${(s.count / maxSource) * 100}%` }} />
                </div>
                <span className="source-count">{s.count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
