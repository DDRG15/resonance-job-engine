import { useEffect, useState } from 'react'
import type { ReviewJob } from '../types'

interface Props {
  onClose: () => void
  onChanged: () => void
}

// pila de revisión manual: jobs con title+empresa REALES pero URL
// no verificable (el LLM fallback la produjo mal). En vez de tirarlos, el humano
// corrige la URL (usando la pista del SRP) o los descarta como no-rescatables.
// Motivos de empresa dudosa: el aviso YA está en la lista con su URL real; lo que se
// revisa es el nombre de la empresa. No hay URL que corregir, solo marcarlo como visto.
const MOTIVO_EMPRESA: Record<string, string> = {
  empresa_dudosa_sin_empresa: 'La fuente no publica la empresa; el aviso se conserva sin ese dato.',
  empresa_dudosa_eslogan: 'El campo empresa parece un eslogan o una descripción, no un nombre.',
}

function enlaceWeb(value: string | null): string | null {
  if (!value) return null
  try {
    const parsed = new URL(value)
    return ['https:', 'http:'].includes(parsed.protocol) ? value : null
  } catch {
    return null
  }
}

export default function ReviewPanel({ onClose, onChanged }: Props) {
  const [jobs, setJobs] = useState<ReviewJob[] | null>(null)
  const [stats, setStats] = useState<{ pending: number; resolved: number } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [urlInputs, setUrlInputs] = useState<Record<string, string>>({})
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    fetch('/api/review')
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`)
        return r.json() as Promise<{ jobs: ReviewJob[]; stats: { pending: number; resolved: number } }>
      })
      .then((body) => {
        if (cancelled) return
        setJobs(body.jobs)
        setStats(body.stats)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  async function resolve(hash: string, resolvedUrl: string | null) {
    try {
      const r = await fetch(`/api/review/${hash}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolved_url: resolvedUrl }),
      })
      if (!r.ok) throw new Error(await r.text())
      setReloadKey((k) => k + 1)
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Revisión manual</h3>
        <span className="phase">
          {stats ? `${stats.pending} pendientes · ${stats.resolved} resueltos` : 'Phase 30'}
        </span>
      </div>
      <p>
        Jobs con puesto y empresa reales pero URL que Resonance no pudo verificar: corregí
        la URL (usá la pista del listado) o descartalos si no son rescatables. Los avisos con
        empresa dudosa siguen en la lista; acá solo se marcan como revisados.
      </p>
      <div className="actions" style={{ marginTop: 0, marginBottom: 12 }}>
        <span style={{ flex: 1 }} />
        <button className="btn mini" onClick={onClose}>Cerrar</button>
      </div>

      {error && <div className="error-banner">No se pudo leer la pila: <code>{error}</code></div>}
      {jobs === null && !error && <span className="side-note">Cargando…</span>}

      {jobs !== null && jobs.length === 0 && (
        <div className="empty-state">
          <span className="glyph">✓</span>
          <b>Pila vacía</b>
          Nada pendiente de revisión — Resonance no perdió jobs rescatables.
        </div>
      )}

      {jobs !== null && jobs.length > 0 && (
        <div className="review-list">
          {jobs.map((j) => {
            const motivoEmpresa = j.reason ? MOTIVO_EMPRESA[j.reason] : undefined
            const aviso = enlaceWeb(j.original_url)
            const listado = enlaceWeb(j.srp_url)
            return (
            <div className="review-card" key={j.review_hash}>
              <div className="review-main">
                <b>{j.title}</b>
                <span className="review-meta">
                  {j.company} · {j.source_domain}
                </span>
                {motivoEmpresa && <span className="review-meta">{motivoEmpresa}</span>}
              </div>
              {motivoEmpresa && aviso ? (
                <a className="review-lead" href={aviso} target="_blank" rel="noopener noreferrer">
                  Abrir el aviso ↗
                </a>
              ) : (
                listado && (
                  <a className="review-lead" href={listado} target="_blank" rel="noopener noreferrer">
                    Ver listado (pista) ↗
                  </a>
                )
              )}
              {motivoEmpresa ? (
                <div className="review-actions">
                  <button className="btn mini" onClick={() => resolve(j.review_hash, null)}>
                    Marcar como revisado
                  </button>
                </div>
              ) : (
              <div className="review-actions">
                <input
                  className="text-input"
                  placeholder="Pegá la URL real de la oferta…"
                  value={urlInputs[j.review_hash] ?? ''}
                  onChange={(e) =>
                    setUrlInputs((p) => ({ ...p, [j.review_hash]: e.target.value }))
                  }
                />
                <button
                  className="btn mini primary"
                  disabled={!(urlInputs[j.review_hash]?.trim())}
                  onClick={() => resolve(j.review_hash, urlInputs[j.review_hash].trim())}
                >
                  Guardar URL
                </button>
                <button className="btn mini" onClick={() => resolve(j.review_hash, null)}>
                  Descartar
                </button>
              </div>
              )}
            </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
