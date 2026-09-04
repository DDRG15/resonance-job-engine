import type { TieredJobDto } from '../types'
import { companyIsDirty, fieldConfidence } from './jobQuality'

interface Props {
  tj: TieredJobDto | null
}

const TIER_CLASS: Record<string, string> = {
  'Tier 1': 'b1', 'Tier 2': 'b2', 'Tier 3': 'b3', 'Tier 4': 'b4',
}

function scoreClass(score: number): string {
  if (score >= 80) return 's1'
  if (score >= 50) return 's2'
  return 's3'
}

/**
 * Panel de detalle del aviso seleccionado — la mitad derecha de la vista split.
 *
 * Reemplaza al JobDrawer (overlay modal). El motivo del cambio no es estético:
 * el drawer tapaba la lista, así que comparar dos avisos obligaba a abrir, leer,
 * cerrar y volver a abrir. Al lado, la comparación es mover una flecha.
 *
 * Es el único lugar de la app donde caben las razones del score. Viajan en
 * /api/results dentro del TieredJobDto — no hace falta tocar el backend.
 */
export default function JobDetail({ tj }: Props) {
  if (tj === null) {
    return (
      <div className="detail detail-empty">
        <p>Ningún aviso coincide con los filtros.</p>
      </div>
    )
  }

  const j = tj.job
  const dirty = companyIsDirty(j.company)
  const conf = fieldConfidence(tj)
  const esManual = tj.match_score < 0

  return (
    <div className="detail">
      <div className="detail-top">
        <span className={`badge ${TIER_CLASS[tj.tier] ?? ''}`}>{tj.tier}</span>
        <span className="conf" title={`${conf.present} de ${conf.total} campos presentes`}>
          {conf.notches.map((n) => (
            <i key={n.key} className={`n-${n.state}`} aria-hidden="true" />
          ))}
          <small>{conf.present}/{conf.total}</small>
        </span>
      </div>

      <h3 className="detail-title">
        <a href={j.url} target="_blank" rel="noopener noreferrer">{j.title || '—'}</a>
      </h3>

      {dirty ? (
        <div className="detail-company dirty" title={j.company.replace(/\n/g, ' · ')}>
          Empresa no extraída — el selector de {j.source_domain} devolvió otra cosa
        </div>
      ) : (
        <div className="detail-company">{j.company}</div>
      )}

      {esManual ? (
        <div className="manual detail-manual">MANUAL — salario en texto, sin puntuar</div>
      ) : (
        <div className={`score detail-score ${scoreClass(tj.match_score)}`}>
          <div className="bar">
            <i style={{ width: `${Math.min(100, Math.max(0, tj.match_score))}%` }} />
          </div>
          <b>{tj.match_score}%</b>
        </div>
      )}

      <dl className="detail-fields">
        <div>
          <dt>Ubicación</dt>
          <dd className={j.location_raw ? '' : 'absent'}>
            {j.location_raw || 'el board no la expone'}
          </dd>
        </div>
        <div>
          <dt>Salario</dt>
          <dd className={j.salary_raw ? '' : 'absent'}>
            {j.salary_raw || 'no publicado'}
          </dd>
        </div>
        <div>
          <dt>Fuente</dt>
          <dd>{j.source_domain}</dd>
        </div>
        <div>
          <dt>Capturado</dt>
          <dd>{(j.scraped_at || '').slice(0, 16).replace('T', ' ')}</dd>
        </div>
      </dl>

      {(tj.match_reasons.length > 0 || tj.miss_reasons.length > 0) && (
        <div className="detail-reasons">
          <div className="detail-sub">Por qué {esManual ? 'quedó a revisión' : `sacó ${tj.match_score}`}</div>
          {tj.match_reasons.map((r, i) => (
            <div className="ok" key={`ok-${i}`}>✓ {r}</div>
          ))}
          {tj.miss_reasons.map((r, i) => (
            <div className="bad" key={`bad-${i}`}>✗ {r}</div>
          ))}
        </div>
      )}

      <div className="detail-cta">
        <a className="btn" href={j.url} target="_blank" rel="noopener noreferrer">
          Abrir oferta ↗
        </a>
      </div>
    </div>
  )
}
