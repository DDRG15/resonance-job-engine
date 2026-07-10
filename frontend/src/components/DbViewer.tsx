import { useEffect, useState } from 'react'
import type { RegistryJob } from '../types'

interface Props {
  onClose: () => void
}

const TIER_CLASS: Record<string, string> = {
  'Tier 1': 'b1', 'Tier 2': 'b2', 'Tier 3': 'b3', 'Tier 4': 'b4',
}

// F12 (pedido de el usuario, live test 07-10): acceso directo a la base de datos —
// TODO lo recopilado, crudo, sin estructura de tiers. Responde también F14:
// las corridas del scheduler notifican por Discord pero sus resultados viven
// acá (tier y match_score backfilleados por el matcher).
export default function DbViewer({ onClose }: Props) {
  const [jobs, setJobs] = useState<RegistryJob[] | null>(null)
  const [last24h, setLast24h] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setJobs(null)
    setError(null)
    const params = last24h ? '?limit=500&hours=24' : '?limit=500'
    fetch(`/api/registry/jobs${params}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`)
        return r.json() as Promise<{ jobs: RegistryJob[] }>
      })
      .then((body) => {
        if (!cancelled) setJobs(body.jobs)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [last24h])

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Base de datos — seen registry</h3>
        <span className="phase">crudo · sin filtros de tier</span>
      </div>
      <p>
        Todo lo que GEMA recopiló, más nuevo primero — incluye las corridas
        automáticas del scheduler.
      </p>
      <div className="actions" style={{ marginTop: 0, marginBottom: 12 }}>
        <button
          className={`btn mini ${last24h ? 'primary' : ''}`}
          onClick={() => setLast24h(true)}
        >
          Últimas 24 h
        </button>
        <button
          className={`btn mini ${!last24h ? 'primary' : ''}`}
          onClick={() => setLast24h(false)}
        >
          Todo (máx. 500)
        </button>
        <span style={{ flex: 1 }} />
        <button className="btn mini" onClick={onClose}>
          Cerrar
        </button>
      </div>

      {error && (
        <div className="error-banner">
          No se pudo leer la base de datos: <code>{error}</code>
        </div>
      )}
      {jobs === null && !error && <span className="side-note">Cargando…</span>}
      {jobs !== null && (
        <>
          <span className="side-note">{jobs.length} filas</span>
          <div className="db-scroll">
            <table className="db-table">
              <thead>
                <tr>
                  <th>Capturado</th>
                  <th>Puesto</th>
                  <th>Empresa</th>
                  <th>Tier</th>
                  <th>Score</th>
                  <th>Ubicación</th>
                  <th>Fuente</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.job_hash}>
                    <td className="mono">{j.scraped_at?.slice(0, 16).replace('T', ' ')}</td>
                    <td>
                      <a href={j.url} target="_blank" rel="noopener noreferrer">
                        {j.title || '—'}
                      </a>
                    </td>
                    <td>{j.company || '—'}</td>
                    <td>
                      {j.tier ? (
                        <span className={`badge ${TIER_CLASS[j.tier] ?? ''}`}>{j.tier}</span>
                      ) : (
                        <span className="side-note">sin tier</span>
                      )}
                    </td>
                    <td className="mono">{j.match_score ?? '—'}</td>
                    <td>{j.location_raw || '—'}</td>
                    <td className="mono">{j.source_domain}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
