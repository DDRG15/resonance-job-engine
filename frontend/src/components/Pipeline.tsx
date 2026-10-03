import { useEffect, useState } from 'react'
import type { Application, ApplicationsResponse } from '../types'

interface Props {
  onClose: () => void
  // Avisa a quien muestra el contador de vencidas (el badge del sidebar) que algo
  // cambió acá. Sin esto el badge diría «3» hasta el próximo refresco de la página,
  // aunque la tercera ya se hubiera atendido hace un minuto.
  onChanged?: () => void
}

// Mismo tope que el backend (database.MAX_PROXIMA_ACCION): el input corta antes de
// que el servidor tenga que contestar 422.
const MAX_ACCION = 200

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
export default function Pipeline({ onClose, onChanged }: Props) {
  const [data, setData] = useState<ApplicationsResponse | null>(null)
  // Fase 9: qué postulación se está editando y el borrador de sus dos campos.
  const [editando, setEditando] = useState<string | null>(null)
  const [accion, setAccion] = useState('')
  const [fecha, setFecha] = useState('')
  const [guardando, setGuardando] = useState(false)
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
      // Cerrar una postulación la saca de las vencidas: el badge tiene que enterarse.
      onChanged?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setMoviendo(null)
    }
  }

  function editar(a: Application) {
    setError(null)
    setEditando(a.app_id)
    setAccion(a.next_action ?? '')
    setFecha(a.follow_up_at ?? '')
  }

  async function guardarAccion(a: Application) {
    setGuardando(true)
    setError(null)
    try {
      // Vacío viaja como null: el backend distingue «no tocar» (campo ausente) de
      // «borrar» (null), y un input vaciado a propósito tiene que borrar.
      const r = await fetch(`/api/applications/${a.app_id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          next_action: accion.trim() || null,
          follow_up_at: fecha || null,
        }),
      })
      if (!r.ok) {
        const cuerpo = await r.json().catch(() => ({}))
        throw new Error(
          typeof cuerpo.detail === 'string' ? cuerpo.detail : `HTTP ${r.status}`)
      }
      setEditando(null)
      await cargar()
      onChanged?.()
    } catch (e) {
      // El formulario queda abierto con lo escrito: perder el texto por un 422 sería
      // castigar al usuario por el error del servidor.
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setGuardando(false)
    }
  }

  const apps = data?.applications ?? []
  const vencidas = data?.vencidas ?? 0
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
        <span className="phase">
          {apps.length} en total
          {vencidas > 0 && (
            <strong className="vencidas-count">
              {' · '}{vencidas} {vencidas === 1 ? 'vencida' : 'vencidas'}
            </strong>
          )}
        </span>
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
                {editando === a.app_id ? (
                  <form
                    className="app-edit"
                    onSubmit={(ev) => { ev.preventDefault(); void guardarAccion(a) }}
                  >
                    <input
                      type="text"
                      className="res-search"
                      aria-label="Próxima acción"
                      placeholder="Ej.: escribir al recruiter"
                      maxLength={MAX_ACCION}
                      value={accion}
                      onChange={(ev) => setAccion(ev.target.value)}
                    />
                    <input
                      type="date"
                      className="res-search"
                      aria-label="Fecha de la próxima acción"
                      value={fecha}
                      onChange={(ev) => setFecha(ev.target.value)}
                    />
                    <button type="submit" className="btn mini primary" disabled={guardando}>
                      Guardar
                    </button>
                    <button type="button" className="btn mini" disabled={guardando}
                            onClick={() => setEditando(null)}>
                      Cancelar
                    </button>
                  </form>
                ) : (
                  // Una cerrada ya no tiene nada que atender: no se muestra su
                  // próxima acción ni se ofrece editarla (el dato queda en la base).
                  !terminales.has(a.status) && (a.next_action || a.follow_up_at) && (
                    <div className={`app-next${a.vencida ? ' vencida' : ''}`}>
                      {a.vencida && <span className="badge b3">Vencida</span>}
                      <span className="app-next-texto">{a.next_action ?? 'Sin texto'}</span>
                      {a.follow_up_at && (
                        <time dateTime={a.follow_up_at}>{a.follow_up_at}</time>
                      )}
                    </div>
                  )
                )}
              </div>
              <div className="app-acciones">
                {!terminales.has(a.status) && editando !== a.app_id && (
                  <button
                    className="btn mini"
                    disabled={moviendo === a.app_id}
                    onClick={() => editar(a)}
                  >
                    {a.next_action || a.follow_up_at ? 'Editar acción' : 'Próxima acción'}
                  </button>
                )}
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
