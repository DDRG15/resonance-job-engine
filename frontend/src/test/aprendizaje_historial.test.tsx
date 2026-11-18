/**
 * Pipeline — Aprendizaje (Fase 10) e Historial por postulación.
 * El historial se pide recién al hacer clic; un error de red se muestra, no rompe.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import Pipeline from '../components/Pipeline'
import type { Application } from '../types'

const APP: Application = {
  app_id: 'a1', job_hash: 'h1', url: 'https://x.com/1', title: 'Backend Engineer',
  company: 'GitLab', source_domain: 'boards.greenhouse.io', tier_al_aplicar: 'Tier 1',
  score_al_aplicar: 92, status: 'postulado', created_at: '2026-09-01',
  stage_changed_at: '2026-09-01', applied_at: '2026-09-01', closed_at: null,
  salary_offered: null, follow_up_at: null, next_action: null, vencida: false,
  notes: null,
}
const MAQUINA = {
  estados: ['borrador', 'postulado', 'screening'],
  cadena: ['borrador', 'postulado', 'screening'],
  terminales: ['rechazado'],
}
const APRENDIZAJE = {
  activo: false, habilitado: true, resultados: 4, umbral: 15, faltan: 11, tope: 8,
  mensaje: 'aprendizaje inactivo: 4 de 15 postulaciones con resultado', error: null,
  score_version: null,
  reglas: [{ rasgo: 'empresa', clave: 'gitlab', entrevistas: 2, ofertas: 0,
    descartes: 0, puntos: 6 }],
}

function resp(body: unknown, ok = true, status = 200) {
  return Promise.resolve({
    ok, status, json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response)
}

function servidor(historial: () => Promise<Response>,
                  stats: (url: string) => Promise<Response> = () => resp({ grupos: [] })) {
  const f = vi.fn((url: string) => {
    if (url.startsWith('/api/aprendizaje')) return resp(APRENDIZAJE)
    if (url.endsWith('/history')) return historial()
    if (url.startsWith('/api/applications/stats')) return stats(url)
    return resp({ applications: [APP], vencidas: 0, ...MAQUINA })
  })
  vi.stubGlobal('fetch', f)
  return f
}

describe('Pipeline — aprendizaje e historial', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('aprendizaje inactivo muestra «N de 15» y las reglas con sus puntos', async () => {
    servidor(() => resp({}))
    render(<Pipeline onClose={() => {}} />)
    expect(await screen.findByText(/Inactivo: 4 de 15 postulaciones con resultado/))
      .toBeTruthy()
    expect(screen.getByText(/empresa: gitlab/)).toBeTruthy()
    expect(screen.getByText(/\+6 pts/)).toBeTruthy()
  })

  it('el historial se pide recién al hacer clic y muestra los estados', async () => {
    const f = servidor(() => resp({ app_id: 'a1', eventos: [
      { id: 1, app_id: 'a1', desde: null, hasta: 'borrador', ocurrio_en: '2026-09-01', nota: null },
      { id: 2, app_id: 'a1', desde: 'borrador', hasta: 'postulado', ocurrio_en: '2026-09-02', nota: null },
    ] }))
    render(<Pipeline onClose={() => {}} />)
    const boton = await screen.findByRole('button', { name: 'Historial' })
    expect(f.mock.calls.some(([u]) => String(u).endsWith('/history'))).toBe(false)
    fireEvent.click(boton)
    expect(await screen.findByText('2026-09-02')).toBeTruthy()
    expect(screen.getByText('2026-09-01')).toBeTruthy()
    expect(f.mock.calls.filter(([u]) => String(u).endsWith('/history'))).toHaveLength(1)
  })

  it('error de red en el historial muestra un mensaje, sin crash', async () => {
    servidor(() => Promise.reject(new Error('Failed to fetch')))
    render(<Pipeline onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Historial' }))
    await waitFor(() =>
      expect(screen.getByText(/No se pudo cargar el historial: Failed to fetch/)).toBeTruthy())
  })

  it.each(['respuesta', 'error'])('ignora una %s tardía de la agrupación anterior', async (tipo) => {
    let resolver!: (r: Response) => void
    let rechazar!: (e: Error) => void
    const lenta = new Promise<Response>((resolve, reject) => {
      resolver = resolve
      rechazar = reject
    })
    const grupo = (nombre: string) => ({ grupos: [
      { grupo: nombre, postulaciones: 2, convirtieron: 1, tasa: 0.5 },
    ] })
    const f = servidor(() => resp({}), (url) => {
      if (url.endsWith('source_domain')) return lenta
      return resp(grupo(url.endsWith('rango_score') ? 'Rango vigente' : 'Tier inicial'))
    })
    render(<Pipeline onClose={() => {}} />)
    await screen.findByText('Tier inicial')
    const selector = screen.getByRole('combobox', { name: 'Agrupar conversión por' })
    fireEvent.change(selector, { target: { value: 'source_domain' } })
    fireEvent.change(selector, { target: { value: 'rango_score' } })
    await screen.findByText('Rango vigente')
    await act(async () => {
      if (tipo === 'error') rechazar(new Error('Fallo anterior'))
      else resolver(await resp(grupo('Board anterior')))
    })
    expect(screen.getByText('Rango vigente')).toBeInTheDocument()
    expect(screen.queryByText('Board anterior')).not.toBeInTheDocument()
    expect(screen.queryByText('Fallo anterior')).not.toBeInTheDocument()
    expect(f.mock.calls.filter(([url]) => url === '/api/applications')).toHaveLength(1)
  })
})
