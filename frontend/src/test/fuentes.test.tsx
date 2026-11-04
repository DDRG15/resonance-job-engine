/**
 * Fase 4 (2026-09-30) — fuentes honestas.
 *
 * El panel muestra los tres estados que decide el backend por conteo contra el historial
 * (sospechosa, sin rendimiento, sana) más «sin medir», y el indicador de Notion dice
 * «token inválido» cuando Notion rechazó el token. Todo control visible hace algo.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import FuentesPanel from '../components/FuentesPanel'
import Sidebar from '../components/Sidebar'
import { ToastProvider } from '../toast'
import type { FuenteEstado, FuentesResponse, Status } from '../types'

function fuente(over: Partial<FuenteEstado>): FuenteEstado {
  return {
    fuente: 'x.com', tipo: 'endpoint', estado: 'sana', items: 10, aceptados: 3,
    run_id: 'r1', corrida: '2026-09-30T10:00:00+00:00', historial: [9, 8],
    ultima_con_avisos: '2026-09-30T10:00:00+00:00', corridas_medidas: 3,
    hubo_error: false, motivo: '10 avisos en la última corrida', ...over,
  }
}

const RESPUESTA: FuentesResponse = {
  ventana_corridas: 5,
  resumen: { sospechosa: 1, sin_rendimiento: 1, sin_medir: 1, sana: 1 },
  fuentes: [
    fuente({ fuente: 'cae.com', estado: 'sospechosa', items: 0, historial: [38, 35, 40],
             ultima_con_avisos: '2026-09-27T10:00:00+00:00',
             motivo: 'cayó a 0 avisos; en las 3 corridas medidas anteriores llegó a traer 40' }),
    fuente({ fuente: 'trueup.io', tipo: 'css', estado: 'sin_rendimiento', items: 0,
             historial: [0, 0], ultima_con_avisos: null, motivo: 'nunca produjo un aviso en 3 corrida(s) medida(s)' }),
    fuente({ fuente: 'vieja.com', tipo: 'css', estado: 'sin_medir', items: null,
             historial: [], ultima_con_avisos: null, motivo: 'la última corrida es anterior a la medición' }),
    fuente({ fuente: 'greenhouse:gitlab', tipo: 'ats', estado: 'sana', items: 120 }),
  ],
}

function respuesta(body: unknown, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body), text: async () => '' } as Response)
}

describe('FuentesPanel', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('renderiza los tres estados que decide el backend, y «sin medir», con su razón', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respuesta(RESPUESTA)))
    render(<FuentesPanel onClose={() => {}} />)

    const cae = await screen.findByTestId('fuente-cae.com')
    expect(within(cae).getByText(/Sospechosa/)).toBeInTheDocument()
    expect(within(cae).getByText('38 · 35 · 40')).toBeInTheDocument()
    expect(within(cae).getByText(/cayó a 0 avisos/)).toBeInTheDocument()

    expect(within(screen.getByTestId('fuente-trueup.io')).getByText(/Sin rendimiento/)).toBeInTheDocument()
    expect(within(screen.getByTestId('fuente-greenhouse:gitlab')).getByText(/Sana/)).toBeInTheDocument()
    const vieja = screen.getByTestId('fuente-vieja.com')
    expect(within(vieja).getByText(/Sin medir/)).toBeInTheDocument()
    expect(within(vieja).getAllByText('—')).toHaveLength(3)  // items null, sin historial, sin último aviso: ningún 0 inventado
  })

  it('el resumen cuenta cada estado y dice la ventana', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respuesta(RESPUESTA)))
    render(<FuentesPanel onClose={() => {}} />)
    const resumen = await screen.findByTestId('resumen-fuentes')
    expect(resumen.textContent).toContain('Sospechosa: 1')
    expect(resumen.textContent).toContain('Sin rendimiento: 1')
    expect(screen.getByText(/ventana de 5 corridas/)).toBeInTheDocument()
  })

  it('«Solo problemas» filtra de verdad y «Ver todas» lo revierte', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respuesta(RESPUESTA)))
    render(<FuentesPanel onClose={() => {}} />)
    await screen.findByTestId('fuente-cae.com')

    fireEvent.click(screen.getByRole('button', { name: 'Solo problemas' }))
    expect(screen.getByTestId('fuente-cae.com')).toBeInTheDocument()
    expect(screen.getByTestId('fuente-trueup.io')).toBeInTheDocument()
    expect(screen.queryByTestId('fuente-greenhouse:gitlab')).toBeNull()
    expect(screen.queryByTestId('fuente-vieja.com')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Ver todas' }))
    expect(screen.getByTestId('fuente-greenhouse:gitlab')).toBeInTheDocument()
  })

  it('«Cerrar» llama a onClose', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respuesta(RESPUESTA)))
    const onClose = vi.fn()
    render(<FuentesPanel onClose={onClose} />)
    await screen.findByTestId('fuente-cae.com')
    fireEvent.click(screen.getByRole('button', { name: 'Cerrar' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('sin trazas dice que hay que correr una extracción', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respuesta({
      fuentes: [], ventana_corridas: 5, resumen: { sospechosa: 0, sin_rendimiento: 0, sin_medir: 0, sana: 0 },
    })))
    render(<FuentesPanel onClose={() => {}} />)
    expect(await screen.findByText('Sin trazas de fuentes')).toBeInTheDocument()
  })

  it('un error del backend se muestra, no se traga', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respuesta('boom', false, 500)))
    render(<FuentesPanel onClose={() => {}} />)
    expect(await screen.findByText(/No se pudo leer el estado de las fuentes/)).toBeInTheDocument()
  })
})

// ── El indicador de Notion ─────────────────────────────────────────────────

const STATUS: Status = {
  boards: 17, boards_active: 10, stale_selectors: [], providers_configured: 7,
  warnings: [], nominal: true, scheduler: { enabled: false, next_run: null },
  is_running: false, has_results: false, needs_setup: false, ttl_hours: 0,
  integrations: { notion: true, sheets: false },
}

function montarSidebar(status: Status, extra: Partial<{ sospechosas: number }> = {}) {
  vi.stubGlobal('fetch', vi.fn(() => respuesta({})))
  return render(
    <ToastProvider>
      <Sidebar
        status={status} stats={null} vault={[]} scheduler={null} profile={null}
        ttlHours={0} onTtlChange={() => {}} onLoadVault={() => {}} onChanged={() => {}}
        onKill={() => {}} onOpenDb={() => {}} onOpenStats={() => {}} onOpenReview={() => {}}
        onOpenPipeline={() => {}} onOpenDuplicados={() => {}} onOpenFuentes={() => {}}
        fuentesSospechosas={extra.sospechosas ?? 0}
        duplicadosPending={0} reviewPending={0}
      />
    </ToastProvider>,
  )
}

describe('Indicador de Notion', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('token inválido: dice «token inválido», no «activo»', () => {
    montarSidebar({ ...STATUS, integrations: { notion: false, sheets: false },
      notion_estado: { estado: 'invalido', detalle: 'Notion rechazó el token (401)', verificado_en: 'x' } })
    const pill = screen.getByTestId('notion-estado')
    expect(pill.textContent).toContain('token inválido')
    expect(pill.className).toContain('pill-bad')
    expect(pill.title).toContain('401')
  })

  it('token válido: lo dice', () => {
    montarSidebar({ ...STATUS, notion_estado: { estado: 'valido', detalle: 'ok', verificado_en: 'x' } })
    expect(screen.getByTestId('notion-estado').textContent).toContain('token válido')
  })

  it('sin red al arrancar: «no verificado», nunca «inválido»', () => {
    montarSidebar({ ...STATUS, notion_estado: { estado: 'no_verificado', detalle: 'no se pudo verificar (ConnectTimeout)', verificado_en: 'x' } })
    const t = screen.getByTestId('notion-estado').textContent ?? ''
    expect(t).toContain('no verificado')
    expect(t).not.toContain('inválido')
  })

  it('apagado (sin key) o backend viejo sin el campo: no muestra nada', () => {
    const { unmount } = montarSidebar({ ...STATUS, notion_estado: { estado: 'apagado', detalle: '', verificado_en: null } })
    expect(screen.queryByTestId('notion-estado')).toBeNull()
    unmount()
    montarSidebar(STATUS)
    expect(screen.queryByTestId('notion-estado')).toBeNull()
  })
})

describe('Botón «Fuentes» del sidebar', () => {
  it('cuenta las sospechosas y abre el panel', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respuesta({})))
    const onOpen = vi.fn()
    render(
      <ToastProvider>
        <Sidebar
          status={STATUS} stats={{ total_seen: 1, recent_24h: 0, by_tier: {} }} vault={[]}
          scheduler={null} profile={null} ttlHours={0} onTtlChange={() => {}}
          onLoadVault={() => {}} onChanged={() => {}} onKill={() => {}} onOpenDb={() => {}}
          onOpenStats={() => {}} onOpenReview={() => {}} onOpenPipeline={() => {}}
          onOpenDuplicados={() => {}} onOpenFuentes={onOpen} fuentesSospechosas={2}
          duplicadosPending={0} reviewPending={0}
        />
      </ToastProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Fuentes (2)' }))
    await waitFor(() => expect(onOpen).toHaveBeenCalled())
  })
})

describe('Píldora de la barra lateral', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('sin avisos dice «Configuración en orden», no «Todo en orden», aunque haya selectores viejos', () => {
    // La tarjeta de estado dice «Atención» con selectores sin verificar: la barra
    // lateral solo mira la configuración y no puede afirmar que todo está bien.
    montarSidebar({ ...STATUS, stale_selectors: ['arc.dev'], nominal: false })
    expect(screen.getByText(/Configuración en orden/)).toBeInTheDocument()
    expect(screen.queryByText(/Todo en orden/)).toBeNull()
  })
})
