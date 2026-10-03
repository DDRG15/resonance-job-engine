import { useEffect, useState } from 'react'
import { getFuentes } from '../api'
import type { EstadoFuente, FuenteEstado, FuentesResponse } from '../types'

interface Props {
  onClose: () => void
}

// El estado se dice con glifo Y texto, no solo con color: la paleta de tiers está
// validada para daltonismo, pero un estado que solo existe como tono es un estado que
// parte de los usuarios no ve.
const ESTADOS: Record<EstadoFuente, { glifo: string; etiqueta: string; ayuda: string }> = {
  sospechosa: {
    glifo: '▲', etiqueta: 'Sospechosa',
    ayuda: 'Trajo avisos en corridas recientes y hoy trae cero: algo que se leía ya no se lee.',
  },
  sin_rendimiento: {
    glifo: '■', etiqueta: 'Sin rendimiento',
    ayuda: 'No trajo avisos en la ventana reciente.',
  },
  sin_medir: {
    glifo: '○', etiqueta: 'Sin medir',
    ayuda: 'La última corrida es anterior a la medición de avisos.',
  },
  sana: { glifo: '●', etiqueta: 'Sana', ayuda: 'Trajo avisos en la última corrida.' },
}

const ORDEN: EstadoFuente[] = ['sospechosa', 'sin_rendimiento', 'sin_medir', 'sana']
const PROBLEMAS: EstadoFuente[] = ['sospechosa', 'sin_rendimiento']

function dia(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '—'
}

function Fila({ f }: { f: FuenteEstado }) {
  const e = ESTADOS[f.estado]
  return (
    <tr data-testid={`fuente-${f.fuente}`}>
      <td>
        <span className={`estado-fuente ${f.estado}`} title={e.ayuda}>
          <span aria-hidden="true">{e.glifo}</span> {e.etiqueta}
        </span>
      </td>
      <td>{f.fuente}</td>
      <td className="mono">{f.tipo}</td>
      <td className="mono">{f.items === null ? '—' : f.items}</td>
      <td className="mono">{f.historial.length ? f.historial.join(' · ') : '—'}</td>
      <td className="mono">{dia(f.ultima_con_avisos)}</td>
      <td>{f.motivo}</td>
    </tr>
  )
}

/**
 * Fase 4 — qué fuentes dicen la verdad.
 *
 * El estado lo decide el backend por CONTEO contra el historial de cada fuente (nunca
 * por el código HTTP) y acá solo se muestra. El único control, «Solo problemas», filtra
 * la tabla de verdad: no hay botón que no haga nada.
 */
export default function FuentesPanel({ onClose }: Props) {
  const [data, setData] = useState<FuentesResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [soloProblemas, setSoloProblemas] = useState(false)

  useEffect(() => {
    let cancelado = false
    getFuentes()
      .then((d) => !cancelado && setData(d))
      .catch((e) => !cancelado && setError(e instanceof Error ? e.message : String(e)))
    return () => { cancelado = true }
  }, [])

  const filas = (data?.fuentes ?? []).filter(
    (f) => !soloProblemas || PROBLEMAS.includes(f.estado),
  )

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Estado de las fuentes</h3>
        <span className="phase">
          por conteo contra el historial, no por código HTTP
          {data && ` · ventana de ${data.ventana_corridas} corridas`}
        </span>
      </div>
      <div className="actions" style={{ marginTop: 0, marginBottom: 14 }}>
        <button
          type="button"
          className="btn mini"
          aria-pressed={soloProblemas}
          onClick={() => setSoloProblemas((v) => !v)}
        >
          {soloProblemas ? 'Ver todas' : 'Solo problemas'}
        </button>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn mini" onClick={onClose}>Cerrar</button>
      </div>

      {error && (
        <div className="error-banner">No se pudo leer el estado de las fuentes: <code>{error}</code></div>
      )}
      {data === null && !error && <div className="skeleton" style={{ height: 80 }} />}

      {data && (
        <>
          <div className="tier-legend" data-testid="resumen-fuentes">
            {ORDEN.map((k) => (
              <span key={k}>
                <span className={`estado-fuente ${k}`}>
                  <span aria-hidden="true">{ESTADOS[k].glifo}</span> {ESTADOS[k].etiqueta}
                </span>
                : <b>{data.resumen[k] ?? 0}</b>
              </span>
            ))}
          </div>

          {data.fuentes.length === 0 ? (
            <div className="empty-state">
              <b>Sin trazas de fuentes</b>Corré una extracción: cada board, endpoint y
              empresa ATS deja una traza con los avisos que trajo.
            </div>
          ) : filas.length === 0 ? (
            <div className="empty-state"><b>Ninguna fuente con problemas</b></div>
          ) : (
            <div className="db-scroll">
              <table className="db-table">
                <thead>
                  <tr>
                    <th>Estado</th><th>Fuente</th><th>Tipo</th><th>Avisos</th>
                    <th>Corridas previas</th><th>Último aviso</th><th>Por qué</th>
                  </tr>
                </thead>
                <tbody>{filas.map((f) => <Fila key={f.fuente} f={f} />)}</tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}
