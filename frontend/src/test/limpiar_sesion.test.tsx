/**
 * "Limpiar sesión" no miente, y el último run sobrevive al reinicio (2026-09-30).
 *
 * El botón se llamaba "Kill Switch" y nunca canceló nada; con una corrida
 * activa el backend responde 409, así que el botón se deshabilita y dice por qué.
 * Tras reiniciar el servidor /api/results trae `last_run` (de la DB) y la
 * pantalla lo muestra en vez de quedar vacía en silencio.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from '../App'
import { ToastProvider } from '../toast'

interface Opciones {
  corriendo?: boolean
  gateBusy?: boolean
  lastRun?: Record<string, unknown> | null
}

function servidor(o: Opciones) {
  const rutas: Record<string, unknown> = {
    '/api/status': {
      boards: 17, boards_active: 11, stale_selectors: [], providers_configured: 3,
      warnings: [], nominal: true, scheduler: { enabled: false, next_run: null },
      is_running: !!o.corriendo, scrape_gate_busy: !!o.gateBusy, has_results: false,
      needs_setup: false, ttl_hours: 48, integrations: { notion: false, sheets: false },
    },
    '/api/config': { config: null, audit_report: null, audit_verdict: null },
    '/api/results': {
      run_id: o.corriendo ? 'run-abc' : null, is_running: !!o.corriendo,
      has_results: false, summary: null, tiers: null, last_run: o.lastRun ?? null,
    },
    '/api/registry/stats': { total_seen: 0, recent_24h: 0, by_tier: {} },
    '/api/vault': { entries: [] },
    '/api/scheduler': {
      enabled: false, armed: false, mode: 'cron', interval_hours: 4, cron_days: [],
      cron_hour: 8, cron_minute: 0, is_running: false, next_run: null,
      last_run: 'No runs yet', has_config: false,
    },
    '/api/cv': { profile: null },
    '/api/review': { stats: { pending: 0 } },
    '/api/correlators': { pendientes: [], total: 0 },
  }
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    const ruta = String(url).split('?')[0]
    return Promise.resolve({
      ok: true, status: 200,
      json: async () => rutas[ruta] ?? {},
      text: async () => '',
    } as Response)
  }))
}

class EventSourceFalso {
  onmessage: ((e: MessageEvent) => void) | null = null
  constructor(public url: string) {}
  addEventListener() {}
  close() {}
}

function montar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider><App /></ToastProvider>
    </QueryClientProvider>,
  )
}

describe('Limpiar sesión', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.stubGlobal('EventSource', EventSourceFalso)
    Element.prototype.scrollIntoView = vi.fn()
  })

  it('ya no se llama Kill Switch: la etiqueta dice lo que hace', async () => {
    servidor({})
    montar()
    const boton = await screen.findByRole('button', { name: 'Limpiar sesión' })
    await waitFor(() => expect(boton).toBeEnabled())
    expect(screen.queryByText(/Kill Switch/i)).toBeNull()
  })

  it('con una corrida manual en curso está deshabilitado y explica por qué', async () => {
    servidor({ corriendo: true, gateBusy: true })
    montar()
    const boton = await screen.findByRole('button', { name: 'Limpiar sesión' })
    expect(await screen.findByText(/no se puede cancelar/i)).toBeInTheDocument()
    expect(boton).toBeDisabled()
  })

  it('con el gate tomado por el cron (sin corrida manual) también se deshabilita', async () => {
    servidor({ corriendo: false, gateBusy: true })
    montar()
    const boton = await screen.findByRole('button', { name: 'Limpiar sesión' })
    // Esperar al status: con el gate ocupado el botón no puede quedar habilitado.
    await screen.findByText(/no se puede cancelar/i)
    expect(boton).toBeDisabled()
  })
})

describe('Último run tras reiniciar', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.stubGlobal('EventSource', EventSourceFalso)
    Element.prototype.scrollIntoView = vi.fn()
  })

  it('un run interrumpido se muestra con el mensaje del backend', async () => {
    servidor({
      lastRun: {
        run_id: 'run-x', status: 'interrupted', trigger: 'api', started_at: null,
        completed_at: null, interrupted_at: '2026-09-30T12:00:00+00:00', error: 'x',
        tier_counts: null, results_available: false,
        message: 'La última corrida (api, inicio 2026-09-30 10:00 UTC) se interrumpió.',
      },
    })
    montar()
    const banner = await screen.findByTestId('last-run-banner')
    expect(banner).toHaveTextContent('interrupted')
    expect(banner).toHaveTextContent('se interrumpió')
    expect(banner.className).toContain('error-banner')
  })

  it('sin historial no se pinta nada', async () => {
    servidor({ lastRun: null })
    montar()
    await screen.findByRole('button', { name: 'Limpiar sesión' })
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByTestId('last-run-banner')).toBeNull()
  })
})
