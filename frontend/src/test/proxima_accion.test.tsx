/**
 * La próxima acción de una postulación, vista desde el front.
 *
 * Regla del proyecto: no hay UI muerta. Cada control nuevo (editar acción, guardar,
 * cancelar, el badge del sidebar) tiene su test, y los que escriben verifican que el
 * PATCH sale de verdad con el cuerpo correcto, no que el botón exista.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from '../App'
import Pipeline from '../components/Pipeline'
import Sidebar from '../components/Sidebar'
import { ToastProvider } from '../toast'
import type { Application, RegistryStats } from '../types'

function app(over: Partial<Application>): Application {
  return {
    app_id: 'a1', job_hash: 'h1', url: 'https://x.com/1', title: 'Backend Engineer',
    company: 'GitLab', source_domain: 'boards.greenhouse.io', tier_al_aplicar: 'Tier 1',
    score_al_aplicar: 92, status: 'postulado', created_at: '2026-09-01',
    stage_changed_at: '2026-09-01', applied_at: '2026-09-01', closed_at: null,
    salary_offered: null, follow_up_at: null, next_action: null, vencida: false,
    notes: null, ...over,
  }
}

const MAQUINA = {
  estados: ['borrador', 'postulado', 'screening', 'tecnica', 'final', 'oferta',
    'rechazado', 'sin_respuesta', 'retirado', 'aceptado'],
  cadena: ['borrador', 'postulado', 'screening', 'tecnica', 'final', 'oferta'],
  terminales: ['aceptado', 'rechazado', 'retirado', 'sin_respuesta'],
}

function respuesta(body: unknown, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body) } as Response)
}

/** Un servidor falso que sirve la lista, las stats y registra los PATCH. */
function servidor(lista: () => Application[], patch?: (body: unknown) => Promise<Response>) {
  const patches: { url: string; body: Record<string, unknown> }[] = []
  const f = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === 'PATCH') {
      patches.push({ url, body: JSON.parse(init.body as string) })
      return patch ? patch(JSON.parse(init.body as string)) : respuesta({})
    }
    if (url.startsWith('/api/applications/stats')) return respuesta({ grupos: [] })
    const apps = lista()
    return respuesta({
      applications: apps, vencidas: apps.filter((a) => a.vencida).length, ...MAQUINA,
    })
  })
  vi.stubGlobal('fetch', f)
  return { patches, f }
}

describe('Pipeline — próxima acción (Fase 9)', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('muestra la próxima acción y su fecha', async () => {
    servidor(() => [app({ next_action: 'escribir al recruiter', follow_up_at: '2099-01-01' })])
    render(<Pipeline onClose={() => {}} />)
    expect(await screen.findByText('escribir al recruiter')).toBeInTheDocument()
    expect(screen.getByText('2099-01-01')).toBeInTheDocument()
    expect(screen.queryByText('Vencida')).toBeNull()
  })

  it('marca como vencida la que el backend marca vencida, y cuenta cuántas son', async () => {
    servidor(() => [
      app({ app_id: 'a1', url: 'https://x.com/1', next_action: 'escribir al recruiter',
            follow_up_at: '2020-01-01', vencida: true }),
      app({ app_id: 'a2', url: 'https://x.com/2', title: 'Data Engineer',
            next_action: 'llamar', follow_up_at: '2099-01-01', vencida: false }),
    ])
    render(<Pipeline onClose={() => {}} />)
    expect(await screen.findByText('escribir al recruiter')).toBeInTheDocument()
    // Exactamente UNA marca: la futura no se pinta como vencida.
    expect(screen.getAllByText('Vencida')).toHaveLength(1)
    expect(screen.getByText(/1 vencida$/)).toBeInTheDocument()
  })

  it('el front no recalcula: sin la marca del backend no pinta vencida aunque la fecha sea vieja', async () => {
    servidor(() => [app({ next_action: 'x', follow_up_at: '2020-01-01', vencida: false })])
    render(<Pipeline onClose={() => {}} />)
    await screen.findByText('x')
    expect(screen.queryByText('Vencida')).toBeNull()
  })

  it('una postulación cerrada no muestra la acción ni ofrece editarla', async () => {
    servidor(() => [app({ status: 'rechazado', next_action: 'dato viejo',
                          follow_up_at: '2020-01-01', vencida: false })])
    render(<Pipeline onClose={() => {}} />)
    await screen.findByText('Rechazado')
    expect(screen.queryByText('dato viejo')).toBeNull()
    expect(screen.queryByText('Editar acción')).toBeNull()
    expect(screen.queryByText('Próxima acción')).toBeNull()
  })

  it('sin acción ofrece «Próxima acción»; con acción ofrece «Editar acción»', async () => {
    servidor(() => [
      app({ app_id: 'a1', url: 'https://x.com/1' }),
      app({ app_id: 'a2', url: 'https://x.com/2', next_action: 'llamar' }),
    ])
    render(<Pipeline onClose={() => {}} />)
    expect(await screen.findByText('Próxima acción')).toBeInTheDocument()
    expect(screen.getByText('Editar acción')).toBeInTheDocument()
  })

  it('editar manda el PATCH con el texto y la fecha, y avisa que algo cambió', async () => {
    let estado = app({})
    const { patches } = servidor(() => [estado], async (body) => {
      const b = body as { next_action: string | null; follow_up_at: string | null }
      estado = app({ next_action: b.next_action, follow_up_at: b.follow_up_at, vencida: true })
      return respuesta(estado)
    })
    const onChanged = vi.fn()
    render(<Pipeline onClose={() => {}} onChanged={onChanged} />)

    fireEvent.click(await screen.findByText('Próxima acción'))
    fireEvent.change(screen.getByLabelText('Próxima acción'),
      { target: { value: '  escribir al recruiter  ' } })
    fireEvent.change(screen.getByLabelText('Fecha de la próxima acción'),
      { target: { value: '2020-02-02' } })
    fireEvent.click(screen.getByText('Guardar'))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(patches).toHaveLength(1)
    expect(patches[0].url).toBe('/api/applications/a1')
    expect(patches[0].body).toEqual({
      next_action: 'escribir al recruiter', follow_up_at: '2020-02-02' })
    // Y la vista se recargó con lo guardado, ya marcada vencida.
    expect(await screen.findByText('Vencida')).toBeInTheDocument()
    expect(screen.queryByLabelText('Próxima acción')).toBeNull()
  })

  it('vaciar los campos manda null para que el backend los borre', async () => {
    const { patches } = servidor(
      () => [app({ next_action: 'llamar', follow_up_at: '2099-01-01' })])
    render(<Pipeline onClose={() => {}} />)
    fireEvent.click(await screen.findByText('Editar acción'))
    expect((screen.getByLabelText('Próxima acción') as HTMLInputElement).value).toBe('llamar')
    expect((screen.getByLabelText('Fecha de la próxima acción') as HTMLInputElement).value)
      .toBe('2099-01-01')
    fireEvent.change(screen.getByLabelText('Próxima acción'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('Fecha de la próxima acción'), { target: { value: '' } })
    fireEvent.click(screen.getByText('Guardar'))
    await waitFor(() => expect(patches).toHaveLength(1))
    expect(patches[0].body).toEqual({ next_action: null, follow_up_at: null })
  })

  it('un 422 del backend se muestra tal cual y el formulario conserva lo escrito', async () => {
    servidor(() => [app({})], async () =>
      respuesta({ detail: 'follow_up_at debe ser una fecha YYYY-MM-DD' }, false, 422))
    render(<Pipeline onClose={() => {}} />)
    fireEvent.click(await screen.findByText('Próxima acción'))
    fireEvent.change(screen.getByLabelText('Próxima acción'), { target: { value: 'mi texto' } })
    fireEvent.click(screen.getByText('Guardar'))
    expect(await screen.findByText('follow_up_at debe ser una fecha YYYY-MM-DD'))
      .toBeInTheDocument()
    expect((screen.getByLabelText('Próxima acción') as HTMLInputElement).value).toBe('mi texto')
  })

  it('Cancelar cierra el formulario sin llamar al backend', async () => {
    const { patches } = servidor(() => [app({})])
    render(<Pipeline onClose={() => {}} />)
    fireEvent.click(await screen.findByText('Próxima acción'))
    fireEvent.change(screen.getByLabelText('Próxima acción'), { target: { value: 'x' } })
    fireEvent.click(screen.getByText('Cancelar'))
    expect(screen.queryByLabelText('Próxima acción')).toBeNull()
    expect(patches).toHaveLength(0)
  })

  it('mover de estado también avisa, porque cerrar una vencida baja el badge', async () => {
    const { patches } = servidor(() => [app({ next_action: 'x', follow_up_at: '2020-01-01',
                                              vencida: true })])
    const onChanged = vi.fn()
    render(<Pipeline onClose={() => {}} onChanged={onChanged} />)
    fireEvent.click(await screen.findByText('Rechazado'))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(patches[0].body).toEqual({ status: 'rechazado' })
  })
})

describe('Sidebar — badge de Postulaciones (Fase 9)', () => {
  const STATS: RegistryStats = { total_seen: 1, recent_24h: 0, by_tier: {} } as RegistryStats

  function montar(overdueApps?: number, onOpenPipeline = vi.fn()) {
    render(
      <ToastProvider>
        <Sidebar
          status={null} stats={STATS} vault={[]} scheduler={null} profile={null}
          ttlHours={0} onTtlChange={() => {}} onLoadVault={() => {}} onChanged={() => {}}
          onKill={() => {}} onOpenDb={() => {}} onOpenStats={() => {}}
          onOpenReview={() => {}} onOpenPipeline={onOpenPipeline}
          onOpenDuplicados={() => {}} duplicadosPending={0} reviewPending={0}
          onOpenFuentes={() => {}} fuentesSospechosas={0}
          overdueApps={overdueApps}
        />
      </ToastProvider>,
    )
    return onOpenPipeline
  }

  it('cuenta las vencidas', () => {
    montar(3)
    expect(screen.getByLabelText('3 vencidas')).toHaveTextContent('3')
  })

  it('en singular dice «1 vencida»', () => {
    montar(1)
    expect(screen.getByLabelText('1 vencida')).toBeInTheDocument()
  })

  it('con cero no hay badge', () => {
    montar(0)
    expect(screen.queryByLabelText(/vencida/)).toBeNull()
  })

  it('sin la prop tampoco inventa un badge', () => {
    montar(undefined)
    expect(screen.queryByLabelText(/vencida/)).toBeNull()
  })

  it('el botón con badge sigue abriendo Postulaciones', () => {
    const abrir = montar(2)
    fireEvent.click(within(screen.getByLabelText('2 vencidas').closest('button')!)
      .getByText(/Postulaciones/))
    expect(abrir).toHaveBeenCalledTimes(1)
  })
})

describe('App — el badge sale de /api/applications?overdue=true', () => {
  class EventSourceFalso {
    onmessage: ((e: MessageEvent) => void) | null = null
    constructor(public url: string) {}
    addEventListener() {}
    close() {}
  }

  beforeEach(() => {
    vi.restoreAllMocks()
    vi.stubGlobal('EventSource', EventSourceFalso)
    Element.prototype.scrollIntoView = vi.fn()
  })

  it('pide solo las vencidas y pinta el total que devuelve el backend', async () => {
    const rutas: Record<string, unknown> = {
      '/api/status': {
        boards: 17, boards_active: 11, stale_selectors: [], providers_configured: 3,
        warnings: [], nominal: true, scheduler: { enabled: false, next_run: null },
        is_running: false, has_results: false, needs_setup: false, ttl_hours: 48,
        integrations: { notion: false, sheets: false },
      },
      '/api/config': { config: null, audit_report: null, audit_verdict: null },
      '/api/results': { run_id: null, is_running: false, has_results: false,
                        summary: null, tiers: null },
      '/api/registry/stats': { total_seen: 10, recent_24h: 1, by_tier: {} },
      '/api/vault': { entries: [] },
      '/api/scheduler': {
        enabled: false, armed: false, mode: 'cron', interval_hours: 4, cron_days: [],
        cron_hour: 8, cron_minute: 0, is_running: false, next_run: null,
        last_run: 'No runs yet', has_config: false,
      },
      '/api/cv': { profile: null },
      '/api/review': { stats: { pending: 0 } },
      '/api/correlators': { pendientes: [], total: 0 },
      '/api/applications': { applications: [], vencidas: 4, ...MAQUINA },
    }
    const pedidas: string[] = []
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      pedidas.push(String(url))
      return Promise.resolve({
        ok: true, status: 200,
        json: async () => rutas[String(url).split('?')[0]] ?? {},
        text: async () => '',
      } as Response)
    }))

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ToastProvider><App /></ToastProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByLabelText('4 vencidas')).toHaveTextContent('4')
    expect(pedidas).toContain('/api/applications?overdue=true')
  })
})
