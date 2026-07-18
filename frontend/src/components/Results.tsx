import { useState } from 'react'
import type { ResultsResponse, TieredJobDto } from '../types'
import JobDrawer, { type DrawerSelection } from './JobDrawer'

function scoreClass(score: number): string {
  if (score >= 80) return 's1'
  if (score >= 50) return 's2'
  return 's3'
}

function JobCard({
  tj, showScore, onOpen,
}: {
  tj: TieredJobDto
  showScore: boolean
  onOpen: () => void
}) {
  const meta =
    tj.job.source_domain + (tj.job.location_raw ? ` · ${tj.job.location_raw}` : '')
  return (
    // F15b (pulido 07-17): el card entero abre el drawer lateral de detalle
    // (patrón Atera/Kimi) — reemplaza el expand inline <details>. El link del
    // título sigue yendo directo a la oferta (stopPropagation).
    <div
      className="job job-clickable"
      onClick={onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
    >
      <div className="t">
        <a
          href={tj.job.url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
        >
          {tj.job.title || '—'}
        </a>
        <small>{meta}</small>
      </div>
      <div className="c">{tj.job.company || '—'}</div>
      <div className="s">{tj.job.salary_raw || '—'}</div>
      {showScore && tj.match_score >= 0 ? (
        <div className={`score ${scoreClass(tj.match_score)}`}>
          <div className="bar">
            <i style={{ width: `${Math.min(100, Math.max(0, tj.match_score))}%` }} />
          </div>
          <b>{tj.match_score}%</b>
        </div>
      ) : (
        <div className="manual">MANUAL</div>
      )}
    </div>
  )
}

function TierSection({
  jobs, badge, badgeClass, title, showScore = true, onSelect,
}: {
  jobs: TieredJobDto[]
  badge: string
  badgeClass: string
  title: string
  showScore?: boolean
  onSelect: (sel: DrawerSelection) => void
}) {
  if (jobs.length === 0) return null
  return (
    <div className="tier-sec">
      <div className="tier-head">
        <span className={`badge ${badgeClass}`}>{badge}</span>
        <h4>{title}</h4>
        <span className="count">{jobs.length} jobs</span>
      </div>
      {jobs.map((tj) => (
        <JobCard
          tj={tj}
          showScore={showScore}
          key={tj.job.url}
          onOpen={() => onSelect({ tj, badge, badgeClass, tierTitle: title })}
        />
      ))}
    </div>
  )
}

// Fase 4 — resultados por tier + errores del run (fail-loud) + export.
export default function Results({ results }: { results: ResultsResponse }) {
  const [selected, setSelected] = useState<DrawerSelection | null>(null)
  if (!results.tiers) return null
  const { tiers, summary } = results
  const total =
    tiers.tier1.length + tiers.tier2.length + tiers.tier3.length + tiers.tier4.length
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Resultados</h3>
        <span className="phase">Fase 4</span>
      </div>
      {summary && summary.errors.length > 0 && (
        <div className="error-banner">
          {summary.errors.length} error(es) durante el run — los resultados pueden
          estar incompletos:
          {summary.errors.map((e, i) => (
            <div key={i}>
              <code>{e}</code>
            </div>
          ))}
        </div>
      )}
      <TierSection
        jobs={tiers.tier1} badge="Tier 1" badgeClass="b1" title="Alta prioridad"
        onSelect={setSelected}
      />
      <TierSection
        jobs={tiers.tier2} badge="Tier 2" badgeClass="b2" title="Explorar"
        onSelect={setSelected}
      />
      <TierSection
        jobs={tiers.tier3} badge="Tier 3" badgeClass="b3" title="Reciclaje"
        onSelect={setSelected}
      />
      <TierSection
        jobs={tiers.tier4} badge="Tier 4" badgeClass="b4"
        title="Revisión manual — salario en texto" showScore={false}
        onSelect={setSelected}
      />
      {total > 0 && (
        <div className="export">
          <a className="btn" href="/api/export.xlsx" download>
            Exportar Excel — {total} jobs
          </a>
        </div>
      )}
      {selected && <JobDrawer sel={selected} onClose={() => setSelected(null)} />}
    </div>
  )
}
