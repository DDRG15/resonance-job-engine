import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import Header from '../components/Header'
import Results from '../components/Results'
import SchedulerPanel from '../components/SchedulerPanel'
import StatTiles from '../components/StatTiles'
import type { ResultsResponse, SchedulerState, Status, TieredJobDto } from '../types'

function makeJob(over: Partial<TieredJobDto['job']> = {}, tj: Partial<TieredJobDto> = {}): TieredJobDto {
  return {
    job: {
      title: 'Senior Python Backend Engineer',
      company: 'Deel',
      url: 'https://example.com/j1',
      salary_raw: '$120k-150k',
      location_raw: 'remoto worldwide',
      source_domain: 'remoteok.com',
      scraped_at: '2026-07-09T12:00:00Z',
      ...over,
    },
    match_score: 92,
    tier: 'Tier 1',
    match_reasons: ['senior backend'],
    miss_reasons: [],
    ...tj,
  }
}

const RESULTS: ResultsResponse = {
  run_id: 'abc',
  is_running: false,
  has_results: true,
  summary: {
    run_id: 'abc', total_urls_found: 3, skipped_seen: 0, skipped_ttl: 0,
    new_processed: 3, tier1_count: 1, tier2_count: 0, tier3_count: 0,
    domains_aborted: [], errors: ['remoteok.com aborted: timeout'],
    duration_seconds: 214,
  },
  tiers: {
    tier1: [makeJob()],
    tier2: [],
    tier3: [],
    tier4: [
      makeJob(
        { title: 'Backend Developer', company: 'Turing', salary_raw: 'Competitive', url: 'https://example.com/j2' },
        { match_score: -1, tier: 'Tier 4', match_reasons: [], miss_reasons: [] },
      ),
    ],
  },
}

const STATUS: Status = {
  boards: 17,
  boards_active: 10,
  stale_selectors: ['remoteok.com'],
  providers_configured: 7,
  warnings: [],
  nominal: false,
  scheduler: { enabled: false, next_run: null },
  is_running: false,
  has_results: false,
  needs_setup: false,
  ttl_hours: 0,
  integrations: { notion: false, sheets: false },
}

const SCHED: SchedulerState = {
  enabled: true,
  armed: false,
  mode: 'cron',
  interval_hours: 4,
  cron_days: ['tue', 'fri', 'sat'],
  cron_hour: 8,
  cron_minute: 0,
  is_running: false,
  next_run: 'vie 10 jul 08:00',
  last_run: 'No runs yet',
  has_config: true,
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => SCHED,
      text: async () => '',
    }),
  )
})

describe('CommandCenter — F10 Usar mi perfil', () => {
  it('sin perfil el botón no existe (cero UI decorativa)', async () => {
    const { default: CommandCenter } = await import('../components/CommandCenter')
    render(<CommandCenter profile={null} onParsed={() => {}} />)
    expect(screen.queryByRole('button', { name: 'Usar mi perfil' })).toBeNull()
  })

  it('con perfil pre-llena el prompt desde rol + skills, sin disparar nada', async () => {
    const { default: CommandCenter } = await import('../components/CommandCenter')
    render(
      <CommandCenter
        profile={{
          role: 'Python Backend Developer | Data Integrity',
          core_skills: ['Python', 'FastAPI', 'SQL'],
        }}
        onParsed={() => {}}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Usar mi perfil' }))
    const textarea = screen.getByPlaceholderText(/Necesito roles Senior/) as HTMLTextAreaElement
    expect(textarea.value).toContain('Python Backend Developer, Data Integrity')
    expect(textarea.value).toContain('Python, FastAPI, SQL')
    // pre-llenar NO parsea: fetch jamás se llamó
    expect(fetch).not.toHaveBeenCalled()
  })
})

describe('Header', () => {
  it('muestra los datos reales del live status', () => {
    render(<Header status={STATUS} />)
    // FIX 5 live-test 07-18: activos primero, registrados entre paréntesis
    expect(screen.getByText(/10 boards activos \(17 reg\.\)/)).toBeInTheDocument()
    expect(screen.getByText(/1 selectores stale \(activos\)/)).toBeInTheDocument()
    expect(screen.getByText('Atención')).toBeInTheDocument()
  })
})

describe('StatTiles', () => {
  it('renderiza conteos por tier y duración', () => {
    render(<StatTiles results={RESULTS} />)
    expect(screen.getByText('Tier 1')).toBeInTheDocument()
    expect(screen.getByText('Tier 4')).toBeInTheDocument()
    expect(screen.getByText('214')).toBeInTheDocument()
  })
})

describe('Results', () => {
  it('renderiza el job card con link real y score', () => {
    render(<Results results={RESULTS} />)
    const link = screen.getByRole('link', { name: 'Senior Python Backend Engineer' })
    expect(link).toHaveAttribute('href', 'https://example.com/j1')
    expect(screen.getByText('92%')).toBeInTheDocument()
  })

  it('marca MANUAL los jobs con salario en texto (Tier 4)', () => {
    render(<Results results={RESULTS} />)
    expect(screen.getByText('MANUAL')).toBeInTheDocument()
  })

  it('grita los errores del run (fail-loud, AUDIT #1)', () => {
    render(<Results results={RESULTS} />)
    expect(screen.getByText(/1 error\(es\) durante el run/)).toBeInTheDocument()
    expect(screen.getByText(/remoteok.com aborted: timeout/)).toBeInTheDocument()
  })
})

describe('SchedulerPanel (cron B4 — no-negociable)', () => {
  it('muestra los días del cron y hace PUT al togglear un día', () => {
    render(<SchedulerPanel scheduler={SCHED} onChanged={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'L' }))
    expect(fetch).toHaveBeenCalledWith(
      '/api/scheduler',
      expect.objectContaining({ method: 'PUT' }),
    )
    const body = JSON.parse(
      (vi.mocked(fetch).mock.calls[0][1] as RequestInit).body as string,
    )
    expect(body.cron_days).toContain('mon')
    expect(body.cron_days).toContain('tue')
    expect(body.retain_cv).toBe(false)
  })

  it('F3: los controles quedan vivos sin config y el toggle hace PUT (queda armado)', () => {
    render(
      <SchedulerPanel scheduler={{ ...SCHED, enabled: false }} onChanged={() => {}} />,
    )
    // Nada deshabilitado: se puede programar en cualquier momento
    for (const sw of screen.getAllByRole('switch')) {
      expect(sw).toBeEnabled()
    }
    fireEvent.click(screen.getAllByRole('switch')[0])
    expect(fetch).toHaveBeenCalledWith(
      '/api/scheduler',
      expect.objectContaining({ method: 'PUT' }),
    )
  })

  it('F3: muestra el estado Programado cuando está armado sin config', () => {
    render(
      <SchedulerPanel
        scheduler={{ ...SCHED, enabled: false, armed: true }}
        onChanged={() => {}}
      />,
    )
    expect(screen.getByText(/Programado — se activa al confirmar/)).toBeInTheDocument()
  })
})
