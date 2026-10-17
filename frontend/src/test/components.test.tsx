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
    location_verdict: 'PASS',
    location_note: '',
    language_verdict: 'PASS',
    language_note: '',
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
    tier2: [
      makeJob(
        { title: 'Backend Developer', company: 'Turing', salary_raw: 'Competitive', url: 'https://example.com/j2' },
        { match_score: 60, tier: 'Tier 2', salario_no_confirmado: true, match_reasons: [], miss_reasons: [] },
      ),
    ],
    tier3: [],
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
  retain_cv: false,
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
    // activos primero, registrados entre paréntesis
    expect(screen.getByText(/10 boards activos \(17 reg\.\)/)).toBeInTheDocument()
    expect(screen.getByText(/1 selectores stale \(activos\)/)).toBeInTheDocument()
    expect(screen.getByText('Atención')).toBeInTheDocument()
  })
})

describe('StatTiles', () => {
  it('renderiza conteos por tier y duración', () => {
    render(<StatTiles results={RESULTS} />)
    expect(screen.getByText('Tier 1')).toBeInTheDocument()
    expect(screen.getByText('Salario sin confirmar')).toBeInTheDocument()
    expect(screen.getByText('214')).toBeInTheDocument()
  })

  it('Tier 1 dice cuántos tienen la ubicación confirmada, no "aplicar ya"', () => {
    render(<StatTiles results={RESULTS} />)
    expect(screen.queryByText(/aplicar ya/)).toBeNull()
    expect(screen.getByText(/con ubicación confirmada/)).toBeInTheDocument()
  })

  it('cuenta los salarios no confirmados de cualquier tier', () => {
    const conNoConfirmado: ResultsResponse = {
      ...RESULTS,
      tiers: {
        ...RESULTS.tiers!,
        tier3: [makeJob({ url: 'https://example.com/j3', salary_raw: 'DOE' },
                        { match_score: 30, tier: 'Tier 3', salario_no_confirmado: true })],
      },
    }
    render(<StatTiles results={conNoConfirmado} />)
    const tile = screen.getByText('Salario sin confirmar').closest('.tile')!
    expect(tile.querySelector('.n')!.textContent).toBe('2')
  })
})

describe('Results', () => {
  it('usa la calidad del backend para conservar nombres largos y señalar eslóganes cortos', () => {
    const name = 'Department of Customer Services, Open Data and Small and Family Business'
    const results: ResultsResponse = {
      ...RESULTS,
      tiers: {
        tier1: [makeJob({ company: name, empresa_no_confirmada: false })],
        tier2: [makeJob({ company: 'We build your future.', empresa_no_confirmada: true,
                        url: 'https://example.com/slogan' }, { tier: 'Tier 2', match_score: 60 })],
        tier3: [],
      },
    }
    render(<Results results={results} />)
    expect(screen.getAllByText(name).length).toBeGreaterThan(0)
    expect(screen.getByTitle('We build your future.')).toHaveTextContent('sin empresa')
  })

  it('renderiza el job card con link real y score', () => {
    render(<Results results={RESULTS} />)
    const link = screen.getByRole('link', { name: 'Senior Python Backend Engineer' })
    expect(link).toHaveAttribute('href', 'https://example.com/j1')
    expect(screen.getByText('92%')).toBeInTheDocument()
  })

  it('puntúa el salario en texto y lo marca sin crear un tier adicional', () => {
    render(<Results results={RESULTS} />)
    expect(screen.queryByText('MANUAL')).toBeNull()
    expect(screen.getByText('$?')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^T[123] / })).toHaveLength(3)
    expect(screen.queryByRole('button', { name: /^T4/ })).toBeNull()
  })

  it('grita los errores del run (fail-loud, AUDIT #1)', () => {
    render(<Results results={RESULTS} />)
    expect(screen.getByText(/1 error\(es\) durante el run/)).toBeInTheDocument()
    expect(screen.getByText(/remoteok.com aborted: timeout/)).toBeInTheDocument()
  })

  // Los 94 PASS-sin-ubicacion y los 37 PASS-con-ubicacion de la corrida del 17-09
  // se dibujaban identicos. Este test congela los tres estados: si alguien vuelve a
  // colapsar FLAG contra PASS, o PASS-por-evidencia contra PASS-por-ausencia, falla.
  it('distingue FLAG, PASS con ubicacion y PASS sin ubicacion', () => {
    const results: ResultsResponse = {
      ...RESULTS,
      tiers: {
        tier1: [
          makeJob(
            { location_raw: 'USA', url: 'https://example.com/flag' },
            { location_verdict: 'FLAG', location_note: 'USA sin requisito declarado' },
          ),
          makeJob(
            { location_raw: 'Worldwide', url: 'https://example.com/pass' },
            { location_verdict: 'PASS' },
          ),
          makeJob(
            { location_raw: '', url: 'https://example.com/vacio' },
            { location_verdict: 'PASS' },
          ),
        ],
        tier2: [], tier3: [],
      },
    }
    render(<Results results={results} />)

    expect(screen.getByTitle('USA sin requisito declarado')).toBeInTheDocument()
    expect(screen.getByText(/⚑/)).toBeInTheDocument()
    expect(screen.getByText('Worldwide')).toBeInTheDocument()
    expect(
      screen.getByTitle(/pasa por ausencia de dato, no por evidencia/),
    ).toBeInTheDocument()
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

describe('JobDetail — requisitos (Fase 7)', () => {
  const REQS: TieredJobDto['requisitos'] = [
    { requisito: 'Python', estado: 'match', peso: 'requerido', evidencia: 'Python 3+ years' },
    { requisito: 'Kafka', estado: 'missing', peso: 'deseable', evidencia: 'Kafka is a plus' },
  ]

  it('muestra cobertura, estado y la frase de la descripción como evidencia', async () => {
    const { default: JobDetail } = await import('../components/JobDetail')
    render(<JobDetail tj={makeJob({}, { requisitos: REQS, cobertura_requisitos: 0.857 })} />)
    expect(screen.getByText(/tu CV cubre el 86%/)).toBeInTheDocument()
    expect(screen.getByText('✓ en tu CV')).toBeInTheDocument()
    expect(screen.getByText('✗ falta')).toBeInTheDocument()
    expect(screen.getByText('«Kafka is a plus»')).toBeInTheDocument()
  })

  it('con menos de 3 requisitos dice que no entran al puntaje', async () => {
    const { default: JobDetail } = await import('../components/JobDetail')
    render(<JobDetail tj={makeJob({}, { requisitos: REQS, cobertura_requisitos: null })} />)
    expect(screen.getByText(/no entran al puntaje/)).toBeInTheDocument()
  })

  it('sin requisitos (fila vieja o sin descripción) no hay tabla', async () => {
    const { default: JobDetail } = await import('../components/JobDetail')
    const { container } = render(<JobDetail tj={makeJob()} />)
    expect(container.querySelector('.req-table')).toBeNull()
  })
})
