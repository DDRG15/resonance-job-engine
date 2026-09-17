import { useEffect, useState } from 'react'
import type { Application, ApplicationsResponse } from '../types'

interface Props {
  onClose: () => void
}

const ETIQUETA: Record<string, string> = {
  borrador: 'Borrador',
  postulado: 'Postulado',
  screening: 'Screening',
  tecnica: 'Técnica',
  final: 'Final',
  oferta: 'Oferta',
  rechazado: 'Rechazado',
  sin_respuesta: 'Sin respuesta',
  retirado: 'Retirado',
  aceptado: 'Aceptado',
}

// El color sigue la etapa, no el resultado: avanzar es teal, cerrar mal es rojo.
const CLASE: Record<string, string> = {
  borrador: 'b3', postulado: 'b2', screening: 'b2',
  tecnica: 'b1', final: 'b1', oferta: 'b1', aceptado: 'b1',
  rechazado: 'b3', sin_respuesta: 'b3', retirado: 'b4',
}

/**
 * Fase 7 — el embudo de postulaciones.
 *
 * GEMA terminaba en "encontrado". Esta vista es la mitad que faltaba: qué pasó
 * después. Y la métrica de conversión de abajo es la única que puede decir si el
 * score sirve — hoy el matcher se calibra a ojo.
 */
export default function Pipeline({ onClose }: Props) {
  const [data, setData] = useState<ApplicationsResponse | null>(null)
  const [stats, setStats] = useState<{ grupo: string; postulaciones: number;
    convirtieron: number; tasa: number }[] | null>(null)
  const [dimension, setDimension] = useState('tier_al_aplicar')
  const [error, setError] = useState<string | null>(null)
  const [moviendo, setMoviendo] = useState<string | null>(null)

  async function cargar() {
    try {
      const r = await fetch('/api/applications')
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setData(await r.json())
      const s = await fetch(`/api/applications/stats?dimension=${dimension}`)
      if (s.ok) setStats((await s.json()).grupos)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => { void cargar() }, [dimension])

  async function mover(app: Application, hasta: string) {
    setMoviendo(app.app_id)
    setError(null)
    try {
      const r = await fetch(`/api/applications/${app.app_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: hasta }),
      })
      if (!r.ok) {
        // El backend explica POR QUÉ la transición no se puede: mostrarlo tal cual
        // es más útil que un "error al mover" genérico.
        const cuerpo = await r.json().catch(() => ({}))
        throw new Error(cuerpo.detail ?? `HTTP ${r.status}`)
      }
      await cargar()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setMoviendo(null)
    }
  }

  const apps = data?.applications ?? []
  const cadena = data?.cadena ?? []
  const terminales = new Set(data?.terminales ?? [])

  function siguientes(estado: string): string[] {
    if (terminales.has(estado)) return []
    const i = cadena.indexOf(estado)
    const avance = i >= 0 && i + 1 < cadena.length ? [cadena[i + 1]] : []
    return [...avance, ...(data?.terminales ?? [])]
  }

  const porEstado = [...cadena, ...(data?.terminales ?? [])]
    .map((e) => ({ estado: e, jobs: apps.filter((a) => a.status === e) }))
    .filter((g) => g.jobs.length > 0)

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Postulaciones</h3>
        <span className="phase">{apps.length} en total</span>
      </div>

      <div className="actions" style={{ marginTop: 0, marginBottom: 14 }}>
        <span style={{ flex: 1 }} />
        <button className="btn mini" onClick={onClose}>Cerrar</button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {apps.length === 0 && !error && (
        <p className="side-note">
          Todavía no marcaste ninguna. Abrí un aviso en Resultados y usá «Apliqué»
          — a partir de ahí se puede medir si el score predice algo.
        </p>
      )}

      {porEstado.map(({ estado, jobs }) => (
        <div className="tier-sec" key={estado}>
          <div className="tier-head">
            <span className={`badge ${CLASE[estado] ?? ''}`}>{ETIQUETA[estado] ?? estado}</span>
            <span className="count">{jobs.length}</span>
          </div>
          {jobs.map((a) => (
            <div className="app-row" key={a.app_id}>
              <div className="app-main">
                <a href={a.url} target="_blank" rel="noopener noreferrer">
                  {a.title ?? a.url}
                </a>
                <small>
                  {a.company ?? '—'}
                  {a.tier_al_aplicar && ` · ${a.tier_al_aplicar} al aplicar`}
                  {a.score_al_aplicar !== null && ` · ${a.score_al_aplicar} pts`}
                </small>
              </div>
              <div className="app-acciones">
                {siguientes(a.status).map((s) => (
                  <button
                    key={s}
                    className="btn mini"
                    disabled={moviendo === a.app_id}
                    onClick={() => void mover(a, s)}
                  >
                    {ETIQUETA[s] ?? s}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      ))}

      {stats && stats.length > 0 && (
        <div className="conv">
          <div className="tier-head">
            <h4>Conversión</h4>
            <select
              className="res-select"
              value={dimension}
              onChange={(e) => setDimension(e.target.value)}
              aria-label="Agrupar conversión por"
            >
              <option value="tier_al_aplicar">por tier</option>
              <option value="source_domain">por board</option>
              <option value="rango_score">por rango de score</option>
            </select>
          </div>
          <p className="side-note">
            «Convirtió» = llegó a screening o más lejos, medido sobre el historial.
            Un rechazo después de una técnica cuenta como conversión: el aviso valía
            la pena aunque no saliera.
          </p>
          {stats.map((g) => (
            <div className="conv-row" key={g.grupo}>
              <span className="f">{g.grupo}</span>
              <span className="track">
                <i style={{ width: `${Math.round(g.tasa * 100)}%` }} />
              </span>
              <span className="n">
                {Math.round(g.tasa * 100)}% <em>{g.convirtieron}/{g.postulaciones}</em>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
