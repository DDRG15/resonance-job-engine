import { useEffect, useState } from 'react'

export type VeredictoTitulo = 'produce' | 'nunca_produce' | 'evidencia_insuficiente' | 'sin_medir'

export interface EmbudoTitulo {
  titulo: string
  veredicto: VeredictoTitulo
  items_total: number
  ramas_por_corrida: number
  segundos_por_corrida: number
  corridas_medidas: number
}

export interface EmbudoFuente {
  fuente: string
  tipo: 'css' | 'endpoint' | 'ats'
  estado: string
  corridas: number
  corridas_sin_medir: number
  items_total: number | null
  items_por_corrida: number | null
  ramas_medidas: number | null
  ramas_vacias: number | null
}

export interface EmbudoPropuesta {
  titulo: string
  ramas_ahorradas_por_corrida: number
  segundos_ahorrados_por_corrida: number
  evidencia: string
}

export interface EmbudoResponse {
  ventana_corridas: number
  min_corridas_medidas: number
  cobertura: { ramas: number; ramas_medidas: number; ramas_sin_medir: number; corridas: number }
  fuentes: EmbudoFuente[]
  titulos: EmbudoTitulo[]
  propuestas: EmbudoPropuesta[]
  ahorro_si_se_aplican_todas: { titulos: number; ramas_por_corrida: number; segundos_por_corrida: number }
}

const ETIQUETA: Record<VeredictoTitulo, string> = {
  nunca_produce: 'Nunca produce',
  evidencia_insuficiente: 'Poca evidencia',
  sin_medir: 'Sin medir',
  produce: 'Produce',
}

const num = (v: number | null) => (v === null ? '—' : String(v))

/**
 * Fase 5 — embudo por fuente y por título.
 *
 * Propone, no borra: la poda la decide el usuario, así que no hay ningún botón que saque un
 * título. Las filas anteriores a la Fase 4 tienen `items` NULL y la sección lo dice en
 * vez de contarlas como cero (leerlas como vacías propondría podar la búsqueda entera).
 */
export default function EmbudoSection() {
  const [data, setData] = useState<EmbudoResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [verTodos, setVerTodos] = useState(false)

  useEffect(() => {
    let cancelado = false
    fetch('/api/embudo')
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`)
        return r.json() as Promise<EmbudoResponse>
      })
      .then((d) => !cancelado && setData(d))
      .catch((e) => !cancelado && setError(e instanceof Error ? e.message : String(e)))
    return () => { cancelado = true }
  }, [])

  const cabecera = <div className="stats-title">Embudo por fuente y título</div>

  if (error) {
    return (
      <div className="stats-card stats-wide" data-testid="embudo">
        {cabecera}
        <div className="error-banner">No se pudo calcular el embudo: <code>{error}</code></div>
      </div>
    )
  }
  if (data === null) {
    return (
      <div className="stats-card stats-wide" data-testid="embudo">
        {cabecera}
        <div className="skeleton" style={{ height: 60 }} />
      </div>
    )
  }

  const cob = data.cobertura
  const sinMedirTodo = cob.ramas > 0 && cob.ramas_medidas === 0
  const nProducen = data.titulos.filter((t) => t.veredicto === 'produce').length
  const titulos = verTodos ? data.titulos : data.titulos.filter((t) => t.veredicto !== 'produce')

  return (
    <div className="stats-card stats-wide" data-testid="embudo">
      <div className="stats-title">
        Embudo por fuente y título
        <span className="phase"> · últimas {data.ventana_corridas} corridas con ramas CSS</span>
      </div>

      {cob.ramas === 0 ? (
        <div className="empty-state"><b>Sin ramas CSS registradas</b>Corré una extracción.</div>
      ) : (
        <p className="side-note" data-testid="embudo-cobertura">
          {cob.corridas} corrida(s), {cob.ramas} ramas: {cob.ramas_medidas} medidas y{' '}
          {cob.ramas_sin_medir} sin medir (items NULL: anteriores a la Fase 4, o la rama no
          consultó la fuente).
        </p>
      )}

      {sinMedirTodo && (
        <div className="empty-state" data-testid="embudo-sin-medir">
          <b>Nada que proponer todavía</b>
          Ninguna rama tiene avisos medidos, y sin medir no es vacío. Con{' '}
          {data.min_corridas_medidas} corridas medidas ya se puede proponer.
        </div>
      )}

      {cob.ramas > 0 && !sinMedirTodo && (
        <>
          <div className="stats-title" style={{ marginTop: 14 }}>Propuesta de poda</div>
          {data.propuestas.length === 0 ? (
            <p className="side-note" data-testid="embudo-sin-propuestas">
              Ningún título cumple la condición: 0 avisos en todas sus ramas medidas, en al
              menos {data.min_corridas_medidas} corridas.
            </p>
          ) : (
            <>
              <p className="side-note">Propone, no borra: sacar un título de la búsqueda lo decidís vos.</p>
              <ul data-testid="embudo-propuestas">
                {data.propuestas.map((p) => (
                  <li key={p.titulo}>
                    <b>{p.titulo}</b> — ahorra {p.ramas_ahorradas_por_corrida} ramas CSS por
                    corrida (~{p.segundos_ahorrados_por_corrida} s). {p.evidencia}.
                  </li>
                ))}
              </ul>
              <p className="side-note">
                Si se aplican las {data.ahorro_si_se_aplican_todas.titulos}:{' '}
                {data.ahorro_si_se_aplican_todas.ramas_por_corrida} ramas y ~
                {data.ahorro_si_se_aplican_todas.segundos_por_corrida} s menos por corrida.
              </p>
            </>
          )}

          <div className="actions" style={{ marginTop: 10, marginBottom: 6 }}>
            <button type="button" className="btn mini" aria-pressed={verTodos}
                    onClick={() => setVerTodos((v) => !v)}>
              {verTodos ? 'Solo los que no producen' : `Ver también los que producen (${nProducen})`}
            </button>
          </div>
          <div className="db-scroll">
            <table className="db-table">
              <thead>
                <tr><th>Título</th><th>Veredicto</th><th>Avisos</th><th>Ramas/corrida</th>
                    <th>s/corrida</th><th>Corridas medidas</th></tr>
              </thead>
              <tbody>
                {titulos.map((t) => (
                  <tr key={t.titulo} data-testid={`titulo-${t.titulo}`}>
                    <td>{t.titulo}</td>
                    <td>{ETIQUETA[t.veredicto]}</td>
                    <td className="mono">{t.items_total}</td>
                    <td className="mono">{t.ramas_por_corrida}</td>
                    <td className="mono">{t.segundos_por_corrida}</td>
                    <td className="mono">{t.corridas_medidas}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {data.fuentes.length > 0 && (
        <>
          <div className="stats-title" style={{ marginTop: 16 }}>Rendimiento por fuente</div>
          <div className="db-scroll">
            <table className="db-table" data-testid="embudo-fuentes">
              <thead>
                <tr><th>Fuente</th><th>Tipo</th><th>Estado</th><th>Corridas</th><th>Sin medir</th>
                    <th>Avisos</th><th>Por corrida</th><th>Ramas</th><th>Vacías</th></tr>
              </thead>
              <tbody>
                {data.fuentes.map((f) => (
                  <tr key={f.fuente}>
                    <td>{f.fuente}</td><td className="mono">{f.tipo}</td>
                    <td>{f.estado.replace('_', ' ')}</td>
                    <td className="mono">{f.corridas}</td>
                    <td className="mono">{f.corridas_sin_medir}</td>
                    <td className="mono">{num(f.items_total)}</td>
                    <td className="mono">{num(f.items_por_corrida)}</td>
                    <td className="mono">{num(f.ramas_medidas)}</td>
                    <td className="mono">{num(f.ramas_vacias)}</td>
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
