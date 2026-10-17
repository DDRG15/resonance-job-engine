/**
 * Refrescar la pestaña no pierde la búsqueda ni la corrida en curso (2026-09-29).
 *
 * Antes, config y runId arrancaban siempre en null. El
 * backend ya tenía GET /api/config y /api/results devuelve el run_id, y el SSE
 * reenvía el historial a quien se conecta tarde: al front solo le faltaba pedirlos.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from '../App'
import { ToastProvider } from '../toast'

const CONFIG = {
  target_titles: ['Backend Engineer'], must_include: [], must_exclude: [],
  min_salary: null, target_domains: ['arc.dev'], custom_xpath: null, custom_css: null,
}

function servidor(opts: { corriendo: boolean; config: boolean }) {
  const rutas: Record<string, unknown> = {
    '/api/status': {
      boards: 17, boards_active: 11, stale_selectors: [], providers_configured: 3,
      warnings: [], nominal: true, scheduler: { enabled: false, next_run: null },
      is_running: opts.corriendo, has_results: false, needs_setup: false, ttl_hours: 48,
      integrations: { notion: false, sheets: false },
    },
    '/api/config': { config: opts.config ? CONFIG : null, audit_report: null, audit_verdict: null },
    '/api/results': {
      run_id: opts.corriendo ? 'run-abc' : null, is_running: opts.corriendo,
      has_results: false, summary: null, tiers: null,
    },
    '/api/registry/stats': { total_seen: 0, recent_24h: 0, by_tier: {} },
    '/api/vault': { entries: [] },
    '/api/scheduler': {
      enabled: false, armed: false, mode: 'cron', interval_hours: 4, cron_days: [],
      cron_hour: 8, cron_minute: 0, is_running: false, next_run: null,
      last_run: 'No runs yet', has_config: opts.config,
    },
    '/api/cv': { profile: null },
    '/api/review': { stats: { pending: 0 } },
    '/api/correlators': { pendientes: [], total: 0 },
  }
  const llamadas: string[] = []
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    const ruta = String(url).split('?')[0]
    llamadas.push(ruta)
    return Promise.resolve({
      ok: true, status: 200,
      json: async () => rutas[ruta] ?? {},
      text: async () => '',
    } as Response)
  }))
  return llamadas
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

describe('Rehidratación al refrescar', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.stubGlobal('EventSource', EventSourceFalso)
    // jsdom no implementa scrollIntoView y la consola lo llama al pintar logs.
    Element.prototype.scrollIntoView = vi.fn()
  })

  it('una corrida en curso vuelve a mostrar la consola del pipeline', async () => {
    servidor({ corriendo: true, config: true })
    montar()
    expect(await screen.findByText('Consola del pipeline')).toBeInTheDocument()
  })

  it('la búsqueda confirmada del servidor vuelve a aparecer', async () => {
    servidor({ corriendo: false, config: true })
    montar()
    expect(await screen.findByText('Validación human-in-the-loop')).toBeInTheDocument()
  })

  it('sin corrida ni búsqueda no inventa nada', async () => {
    const llamadas = servidor({ corriendo: false, config: false })
    montar()
    await screen.findByText(/.+/, { selector: 'h1, h2, header *' }).catch(() => null)
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('Consola del pipeline')).toBeNull()
    expect(screen.queryByText('Validación human-in-the-loop')).toBeNull()
    expect(llamadas).toContain('/api/config')
  })
})
