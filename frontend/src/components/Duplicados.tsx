import { useEffect, useState } from 'react'
import type { CorrelatorPendiente, CorrelatorsResponse } from '../types'

interface Props {
  onClose: () => void
  onChanged?: () => void
}

function dia(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '—'
}

/**
 * Fase 11 — pares de avisos que el correlator cree que son el mismo puesto y no se
 * anima a decidir solo (confianza baja: mismo título y empresa, días distintos).
 *
 * Hasta el 2026-09-28 se resolvían por SQL a mano, y había 119 esperando. «Es el
 * mismo» saca al duplicado del shortlist — conectado el mismo día: antes aceptar no
 * cambiaba nada de lo que se veía. «Son distintos» deja a los dos.
 */
export default function Duplicados({ onClose, onChanged }: Props) {
  const [data, setData] = useState<CorrelatorsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [decidiendo, setDecidiendo] = useState<string | null>(null)

  async function cargar() {
    try {
      const r = await fetch('/api/correlators')
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setData(await r.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => { void cargar() }, [])

  async function decidir(p: CorrelatorPendiente, decision: 'ACCEPTED' | 'REJECTED') {
    const clave = p.job_hash + p.hash_duplicado
    setDecidiendo(clave)
    setError(null)
    try {
      const r = await fetch('/api/correlators/decision', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_hash: p.job_hash, hash_duplicado: p.hash_duplicado, decision }),
      })
      if (!r.ok) {
        // El backend dice por qué no (ya decidido, no existe): mostrarlo tal cual.
        const cuerpo = await r.json().catch(() => ({}))
        throw new Error(cuerpo.detail ?? `HTTP ${r.status}`)
      }
      await cargar()
      onChanged?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDecidiendo(null)
    }
  }

  const pendientes = data?.pendientes ?? []

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Duplicados</h3>
        <span className="phase">{pendientes.length} por decidir</span>
      </div>

      <div className="actions" style={{ marginTop: 0, marginBottom: 14 }}>
        <span style={{ flex: 1 }} />
        <button className="btn mini" onClick={onClose}>Cerrar</button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {data && pendientes.length === 0 && !error && (
        <p className="side-note">No hay pares esperando decisión.</p>
      )}

      {pendientes.map((p) => {
        const clave = p.job_hash + p.hash_duplicado
        return (
          <div className="app-row dup-row" key={clave} data-testid="dup-row">
            <div className="app-main">
              <a href={p.can_url ?? undefined} target="_blank" rel="noopener noreferrer">
                {p.can_title ?? p.job_hash}
              </a>
              <small>
                {p.can_company ?? '—'} · {p.can_board ?? '—'} · {dia(p.can_fecha)}
              </small>
              <a href={p.dup_url ?? undefined} target="_blank" rel="noopener noreferrer">
                {p.dup_title ?? p.hash_duplicado}
              </a>
              <small>
                {p.dup_company ?? '—'} · {p.dup_board ?? '—'} · {dia(p.dup_fecha)}
              </small>
              <small className="dup-motivo">
                {p.correlator} · confianza {Math.round(p.confianza * 100)}%
                {p.motivo ? ` · ${p.motivo}` : ''}
              </small>
            </div>
            <div className="app-acciones">
              <button className="btn mini" disabled={decidiendo === clave}
                      onClick={() => void decidir(p, 'ACCEPTED')}>
                Es el mismo
              </button>
              <button className="btn mini" disabled={decidiendo === clave}
                      onClick={() => void decidir(p, 'REJECTED')}>
                Son distintos
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
