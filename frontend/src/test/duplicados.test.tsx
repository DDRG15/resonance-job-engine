import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import Duplicados from '../components/Duplicados'
import type { CorrelatorPendiente } from '../types'

const PAR: CorrelatorPendiente = {
  job_hash: 'h1', hash_duplicado: 'h2', correlator: 'relist_otro_dia',
  confianza: 0.6, motivo: 'mismo titulo y empresa, otro dia', creado_en: '2026-09-20',
  can_title: 'Principal Backend Engineer', can_company: 'Jeeves', can_board: 'remoteok.com',
  can_fecha: '2026-07-09T10:00:00', can_url: 'https://x.com/1', can_ubicacion: null,
  dup_title: 'Principal Backend Engineer', dup_company: 'Jeeves', dup_board: 'remoteok.com',
  dup_fecha: '2026-09-04T10:00:00', dup_url: 'https://x.com/2', dup_ubicacion: null,
}

function respuesta(body: unknown, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body) } as Response)
}

describe('Duplicados (Fase 11 — no dead UI)', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('muestra el par con los dos avisos y su confianza', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respuesta({ pendientes: [PAR], total: 1 })))
    render(<Duplicados onClose={() => {}} />)
    expect(await screen.findByText('1 por decidir')).toBeInTheDocument()
    expect(screen.getAllByText('Principal Backend Engineer')).toHaveLength(2)
    expect(screen.getByText(/confianza 60%/)).toBeInTheDocument()
  })

  it('«Es el mismo» manda ACCEPTED al backend de verdad', async () => {
    const f = vi.fn()
      .mockImplementationOnce(() => respuesta({ pendientes: [PAR], total: 1 }))
      .mockImplementationOnce(() => respuesta({ estado: 'ACCEPTED' }))
      .mockImplementationOnce(() => respuesta({ pendientes: [], total: 0 }))
    vi.stubGlobal('fetch', f)
    const onChanged = vi.fn()
    render(<Duplicados onClose={() => {}} onChanged={onChanged} />)
    fireEvent.click(await screen.findByText('Es el mismo'))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    const [url, init] = f.mock.calls[1] as [string, RequestInit]
    expect(url).toBe('/api/correlators/decision')
    expect(JSON.parse(init.body as string)).toEqual(
      { job_hash: 'h1', hash_duplicado: 'h2', decision: 'ACCEPTED' })
    expect(await screen.findByText('No hay pares esperando decisión.')).toBeInTheDocument()
  })

  it('un 409 se muestra con el mensaje del backend, tal cual', async () => {
    const f = vi.fn()
      .mockImplementationOnce(() => respuesta({ pendientes: [PAR], total: 1 }))
      .mockImplementationOnce(() =>
        respuesta({ detail: 'Ese par ya esta decidido como REJECTED.' }, false, 409))
    vi.stubGlobal('fetch', f)
    render(<Duplicados onClose={() => {}} />)
    fireEvent.click(await screen.findByText('Son distintos'))
    expect(await screen.findByText('Ese par ya esta decidido como REJECTED.')).toBeInTheDocument()
  })
})
