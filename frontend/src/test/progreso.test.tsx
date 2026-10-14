/**
 * Pieza 4 — la barra de progreso de la corrida.
 *
 * Escenario: la corrida dura 10-20 minutos. Lo que el usuario tiene que poder leer de un
 * vistazo es: cuánto va, cuánto falta (aproximado), y que sigue viva. Y cuando el
 * backend no sabe cuánto falta, la UI lo dice en vez de inventar un número.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import ProgresoCorrida from '../components/ProgresoCorrida'
import PipelineConsole from '../components/PipelineConsole'
import type { Progreso } from '../types'

function base(extra: Partial<Progreso> = {}): Progreso {
  return {
    fase: 'navegador', porcentaje: 42, completadas: 21, total: 50,
    transcurrido_s: 192, restante_s: 420, excedido: false,
    ultima_actividad_hace_s: 2, estimado_desde: 'historial',
    ...extra,
  }
}

describe('ProgresoCorrida', () => {
  it('no renderiza nada si no hay corrida', () => {
    const { container } = render(<ProgresoCorrida progreso={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('muestra porcentaje, fase, transcurrido, restante y última actividad', () => {
    render(<ProgresoCorrida progreso={base()} />)
    const barra = screen.getByRole('progressbar')
    expect(barra).toHaveAttribute('aria-valuenow', '42')
    expect(barra).toHaveAttribute('aria-valuemin', '0')
    expect(barra).toHaveAttribute('aria-valuemax', '100')
    expect(screen.getByText('42%')).toBeInTheDocument()
    expect(screen.getByText(/Fase: Recorriendo boards con el navegador/)).toBeInTheDocument()
    expect(screen.getByText('Transcurrido 3:12')).toBeInTheDocument()
    expect(screen.getByText('Restan ~7 min')).toBeInTheDocument()
    expect(screen.getByText('Última actividad hace 2 s')).toBeInTheDocument()
    expect(screen.getByText('21 de 50 unidades')).toBeInTheDocument()
  })

  it('traduce cada fase', () => {
    const { rerender } = render(<ProgresoCorrida progreso={base({ fase: 'endpoints' })} />)
    expect(screen.getByText('Fase: Leyendo APIs…')).toBeInTheDocument()
    rerender(<ProgresoCorrida progreso={base({ fase: 'puntuando' })} />)
    expect(screen.getByText('Fase: Puntuando y guardando…')).toBeInTheDocument()
  })

  it('sin referencia para estimar dice «calculando…» y no un número', () => {
    render(<ProgresoCorrida progreso={base({ restante_s: null, estimado_desde: null })} />)
    expect(screen.getByText('Restante: calculando…')).toBeInTheDocument()
    expect(screen.queryByText(/^Restan /)).not.toBeInTheDocument()
  })

  it('excedido: dice «tardando más de lo habitual», nunca una cuenta regresiva', () => {
    render(<ProgresoCorrida progreso={base({ excedido: true, restante_s: null })} />)
    expect(screen.getByText('Está tardando más de lo habitual')).toBeInTheDocument()
    expect(screen.queryByText(/^Restan /)).not.toBeInTheDocument()
    expect(screen.queryByText(/calculando/)).not.toBeInTheDocument()
  })

  it('un restante corto se muestra como «menos de 1 min»', () => {
    render(<ProgresoCorrida progreso={base({ restante_s: 20 })} />)
    expect(screen.getByText('Restan menos de 1 min')).toBeInTheDocument()
  })

  it('restante cero: «Terminando…»', () => {
    render(<ProgresoCorrida progreso={base({ restante_s: 0, fase: 'puntuando' })} />)
    expect(screen.getByText('Terminando…')).toBeInTheDocument()
  })

  it('con la estimación de respaldo (corridas) avisa que es gruesa', () => {
    render(<ProgresoCorrida progreso={base({ estimado_desde: 'corridas' })} />)
    expect(screen.getByText(/Estimación gruesa/)).toBeInTheDocument()
  })

  it('pasado el umbral de inactividad avisa que puede estar trabado', () => {
    render(<ProgresoCorrida progreso={base({ ultima_actividad_hace_s: 150 })} />)
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Sin actividad hace 2 min — puede estar trabado')
    expect(screen.getByText('Última actividad hace 2 min 30 s')).toBeInTheDocument()
  })

  it('con actividad reciente no hay aviso', () => {
    render(<ProgresoCorrida progreso={base({ ultima_actividad_hace_s: 120 })} />)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('el aviso de inactividad no ofrece ningún botón (no hay cancelación real)', () => {
    render(<ProgresoCorrida progreso={base({ ultima_actividad_hace_s: 300 })} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('listo: 100%, sin restante ni aviso de inactividad', () => {
    render(<ProgresoCorrida
      progreso={base({ fase: 'listo', porcentaje: 100, restante_s: 0,
        ultima_actividad_hace_s: 500 })} />)
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
    expect(screen.getByText('Terminó')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText(/Última actividad/)).not.toBeInTheDocument()
  })

  it('acota el porcentaje a 0-100', () => {
    render(<ProgresoCorrida progreso={base({ porcentaje: 180 })} />)
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
  })
})

// ── Cableado: PipelineConsole recibe el evento SSE `progreso` ──────────────────────

class FakeEventSource {
  static ultimo: FakeEventSource | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {}
  constructor(public url: string) { FakeEventSource.ultimo = this }
  addEventListener(tipo: string, fn: (e: MessageEvent) => void) {
    (this.listeners[tipo] ??= []).push(fn)
  }
  close() {}
  emitir(tipo: string, data: unknown) {
    const ev = { data: JSON.stringify(data) } as MessageEvent
    for (const fn of this.listeners[tipo] ?? []) fn(ev)
  }
}

describe('PipelineConsole + progreso', () => {
  beforeEach(() => {
    // jsdom no implementa scrollIntoView; la consola lo llama al agregar líneas.
    Element.prototype.scrollIntoView = vi.fn()
  })
  afterEach(() => { vi.unstubAllGlobals(); FakeEventSource.ultimo = null })

  it('muestra la barra con el evento `progreso` y la cierra en 100% al `done`', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const onDone = vi.fn()
    render(<PipelineConsole runId="r1" onDone={onDone} />)
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()

    act(() => { FakeEventSource.ultimo!.emitir('progreso', base()) })
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '42')

    act(() => { FakeEventSource.ultimo!.emitir('done', {}) })
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
    expect(screen.getByText('Terminó')).toBeInTheDocument()
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('un evento `progreso` ilegible no rompe la consola', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    render(<PipelineConsole runId="r2" onDone={() => {}} />)
    act(() => {
      for (const fn of FakeEventSource.ultimo!.listeners['progreso'] ?? []) {
        fn({ data: '{no es json' } as MessageEvent)
      }
    })
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByText('Consola del pipeline')).toBeInTheDocument()
  })

  it('si el stream se corta, la barra desaparece en vez de quedar congelada', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    render(<PipelineConsole runId="r3" onDone={() => {}} />)
    act(() => { FakeEventSource.ultimo!.emitir('progreso', base()) })
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    act(() => { FakeEventSource.ultimo!.onerror?.() })
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  })
})
