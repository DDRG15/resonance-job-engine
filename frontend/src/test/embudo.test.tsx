/**
 * Fase 5 (2026-09-30) — la sección de embudo de las estadísticas.
 *
 * Propone y no borra; dice cuántas ramas sin medir hay en lugar de leerlas como cero.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import EmbudoSection, { type EmbudoResponse } from '../components/EmbudoSection'
import StatsBoard from '../components/StatsBoard'

const BASE: EmbudoResponse = {
  ventana_corridas: 5,
  min_corridas_medidas: 2,
  cobertura: { ramas: 12, ramas_medidas: 12, ramas_sin_medir: 0, corridas: 3 },
  fuentes: [
    { fuente: 'a.com', tipo: 'css', estado: 'sana', corridas: 3, corridas_sin_medir: 0,
      items_total: 36, items_por_corrida: 12, ramas_medidas: 9, ramas_vacias: 6 },
    { fuente: 'ep.com', tipo: 'endpoint', estado: 'sin_medir', corridas: 1, corridas_sin_medir: 1,
      items_total: null, items_por_corrida: null, ramas_medidas: null, ramas_vacias: null },
  ],
  titulos: [
    { titulo: 'cobol', veredicto: 'nunca_produce', items_total: 0, ramas_por_corrida: 2,
      segundos_por_corrida: 4, corridas_medidas: 3 },
    { titulo: 'fortran', veredicto: 'evidencia_insuficiente', items_total: 0, ramas_por_corrida: 2,
      segundos_por_corrida: 2, corridas_medidas: 1 },
    { titulo: 'python', veredicto: 'produce', items_total: 48, ramas_por_corrida: 2,
      segundos_por_corrida: 2, corridas_medidas: 3 },
  ],
  propuestas: [
    { titulo: 'cobol', ramas_ahorradas_por_corrida: 2, segundos_ahorrados_por_corrida: 4,
      evidencia: '0 avisos en 6 ramas de 3 corridas medidas, en ninguna fuente' },
  ],
  ahorro_si_se_aplican_todas: { titulos: 1, ramas_por_corrida: 2, segundos_por_corrida: 4 },
}

function servidor(body: unknown, ok = true, status = 200) {
  vi.stubGlobal('fetch', vi.fn(() =>
    Promise.resolve({ ok, status, json: () => Promise.resolve(body), text: async () => 'boom' } as Response)))
}

describe('EmbudoSection', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('muestra la propuesta con cuántas ramas ahorra por corrida', async () => {
    servidor(BASE)
    render(<EmbudoSection />)
    const lista = await screen.findByTestId('embudo-propuestas')
    expect(lista.textContent).toContain('cobol')
    expect(lista.textContent).toContain('ahorra 2 ramas CSS por corrida')
    expect(screen.getByText(/Propone, no borra/)).toBeInTheDocument()
  })

  it('no hay ningún control que borre un título', async () => {
    servidor(BASE)
    render(<EmbudoSection />)
    await screen.findByTestId('embudo-propuestas')
    const botones = screen.getAllByRole('button').map((b) => b.textContent ?? '')
    expect(botones.some((t) => /borrar|eliminar|podar|sacar/i.test(t))).toBe(false)
  })

  it('el botón de «ver también los que producen» funciona', async () => {
    servidor(BASE)
    render(<EmbudoSection />)
    await screen.findByTestId('embudo-propuestas')
    expect(screen.queryByTestId('titulo-python')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Ver también los que producen \(1\)/ }))
    expect(screen.getByTestId('titulo-python')).toBeInTheDocument()
    expect(screen.getByTestId('titulo-fortran').textContent).toContain('Poca evidencia')
  })

  it('con todo sin medir dice que no es vacío y no propone nada', async () => {
    servidor({ ...BASE, cobertura: { ramas: 1230, ramas_medidas: 0, ramas_sin_medir: 1230, corridas: 3 },
               propuestas: [], titulos: [] })
    render(<EmbudoSection />)
    expect(await screen.findByTestId('embudo-sin-medir')).toBeInTheDocument()
    expect(screen.getByTestId('embudo-cobertura').textContent).toContain('1230 sin medir')
    expect(screen.queryByTestId('embudo-propuestas')).toBeNull()
  })

  it('sin propuestas lo dice', async () => {
    servidor({ ...BASE, propuestas: [] })
    render(<EmbudoSection />)
    expect(await screen.findByTestId('embudo-sin-propuestas')).toBeInTheDocument()
  })

  it('el rendimiento por fuente muestra — para lo no medido, nunca 0', async () => {
    servidor(BASE)
    render(<EmbudoSection />)
    const tabla = await screen.findByTestId('embudo-fuentes')
    const fila = Array.from(tabla.querySelectorAll('tr')).find((r) => r.textContent?.includes('ep.com'))!
    const celdas = Array.from(fila.querySelectorAll('td')).map((c) => c.textContent)
    expect(celdas.slice(5)).toEqual(['—', '—', '—', '—'])   // avisos, por corrida, ramas, vacías
  })

  it('un error del backend se muestra', async () => {
    servidor('x', false, 500)
    render(<EmbudoSection />)
    expect(await screen.findByText(/No se pudo calcular el embudo/)).toBeInTheDocument()
  })

  it('queda dentro de la vista de estadísticas', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve({
      ok: true, status: 200, text: async () => '',
      json: async () => (String(url).includes('/api/embudo') ? BASE
        : { by_tier: {}, by_source: [], by_day: [] }),
    } as Response)))
    render(<StatsBoard onClose={() => {}} />)
    expect(await screen.findByTestId('embudo')).toBeInTheDocument()
    expect(await screen.findByTestId('embudo-propuestas')).toBeInTheDocument()
  })
})
