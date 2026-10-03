import type { ResultsResponse } from '../types'

// Fila de stat tiles Pulse — misma data que el summary del engine.
export default function StatTiles({ results }: { results: ResultsResponse }) {
  if (!results.tiers || !results.summary) return null
  const { tiers, summary } = results
  // Tier 1 es buen calce, no una promesa de elegibilidad (auditoria de revision externa del 30-09,
  // punto 5): de 1.177 Tier 1, solo 367 tenian la ubicacion confirmada. El tile dice
  // cuantos lo estan en vez de "aplicar ya".
  const confirmados = tiers.tier1.filter(
    (t) => t.location_verdict === 'PASS' && !!t.job.location_raw,
  ).length
  // Fase 6 (D3): un salario en texto ya no saca el aviso del scoring. Tier 4 solo
  // queda en filas viejas; el tile cuenta lo que hoy significa: salario sin confirmar.
  const sinConfirmar =
    [...tiers.tier1, ...tiers.tier2, ...tiers.tier3].filter((t) => t.salario_no_confirmado)
      .length + tiers.tier4.length
  const tiles = [
    { cls: 't1', sw: 'var(--t1)', k: 'Tier 1', n: tiers.tier1.length,
      d: `80–100 % — ${confirmados} con ubicación confirmada` },
    { cls: '',   sw: 'var(--t2)', k: 'Tier 2', n: tiers.tier2.length, d: '50–79 % — revisar' },
    { cls: '',   sw: 'var(--t3)', k: 'Tier 3', n: tiers.tier3.length, d: '<50 % — reciclaje' },
    { cls: '',   sw: 'var(--t4)', k: 'Salario sin confirmar', n: sinConfirmar,
      d: 'en texto — puntuado sin el eje de salario' },
  ]
  return (
    <div className="tiles">
      {tiles.map((t) => (
        <div className={`tile ${t.cls}`} key={t.k}>
          <div className="k">
            <span className="sw" style={{ background: t.sw }} />
            {t.k}
          </div>
          <div className="n">{t.n}</div>
          <div className="d">{t.d}</div>
        </div>
      ))}
      {summary.duration_seconds !== null && (
        <div className="tile">
          <div className="k">Duración</div>
          <div className="n">
            {Math.round(summary.duration_seconds)}
            <span style={{ fontSize: 15, color: 'var(--muted)' }}>s</span>
          </div>
          <div className="d">corrida completa</div>
        </div>
      )}
    </div>
  )
}
