import { useEffect } from 'react'
import type { TieredJobDto } from '../types'

export interface DrawerSelection {
  tj: TieredJobDto
  badge: string
  badgeClass: string
  tierTitle: string
}

interface Props {
  sel: DrawerSelection
  onClose: () => void
}

function scoreClass(score: number): string {
  if (score >= 80) return 's1'
  if (score >= 50) return 's2'
  return 's3'
}

// F15b (pulido 07-17, patrón Atera/Kimi): panel lateral de detalle al clickear
// un job — reemplaza el expand inline. Puramente presentación: mismos datos del
// TieredJobDto que ya viaja en /api/results, cero lógica nueva.
export default function JobDrawer({ sel, onClose }: Props) {
  const { tj, badge, badgeClass, tierTitle } = sel

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <>
      <div className="drawer-overlay" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label="Detalle del job">
        <div className="drawer-head">
          <span className={`badge ${badgeClass}`}>{badge}</span>
          <span className="drawer-tier-title">{tierTitle}</span>
          <button className="btn mini" onClick={onClose} aria-label="Cerrar">✕</button>
        </div>

        <h3 className="drawer-job-title">{tj.job.title || '—'}</h3>
        <div className="drawer-company">{tj.job.company || '—'}</div>

        {tj.match_score >= 0 ? (
          <div className={`score drawer-score ${scoreClass(tj.match_score)}`}>
            <div className="bar">
              <i style={{ width: `${Math.min(100, Math.max(0, tj.match_score))}%` }} />
            </div>
            <b>{tj.match_score}%</b>
          </div>
        ) : (
          <div className="manual">MANUAL</div>
        )}

        <div className="drawer-fields">
          <div className="drawer-field">
            <span>Salario</span>
            <b>{tj.job.salary_raw || 'no publicado'}</b>
          </div>
          <div className="drawer-field">
            <span>Ubicación</span>
            <b>{tj.job.location_raw || 'no especificada'}</b>
          </div>
          <div className="drawer-field">
            <span>Fuente</span>
            <b>{tj.job.source_domain}</b>
          </div>
          <div className="drawer-field">
            <span>Capturado</span>
            <b>{tj.job.scraped_at}</b>
          </div>
        </div>

        {(tj.match_reasons.length > 0 || tj.miss_reasons.length > 0) && (
          <div className="drawer-reasons">
            <div className="stats-title">Razones del match</div>
            {tj.match_reasons.map((r, i) => (
              <div className="ok" key={`ok-${i}`}>✓ {r}</div>
            ))}
            {tj.miss_reasons.map((r, i) => (
              <div className="bad" key={`bad-${i}`}>✗ {r}</div>
            ))}
          </div>
        )}

        <div className="drawer-cta">
          <a className="btn" href={tj.job.url} target="_blank" rel="noopener noreferrer">
            Abrir oferta ↗
          </a>
        </div>
      </aside>
    </>
  )
}
