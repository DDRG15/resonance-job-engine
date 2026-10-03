/**
 * Puntos 11 y 12 de revision externa (2026-09-30) en la UI: el CV no sale sin aviso previo aceptado,
 * y los controles de perfil dicen la verdad sobre la retencion.
 *
 * Cada test que reproduce la falla original esta escrito contra el contrato final y se
 * corrio ANTES del fix contra b871963 (ver ).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import CVUpload from '../components/CVUpload'
import SchedulerPanel from '../components/SchedulerPanel'
import { ToastProvider } from '../toast'
import type { SchedulerState } from '../types'
import { CV_CONSENT_VERSION } from '../consent'

const PERFIL = { role: 'Backend Engineer', core_skills: ['python'], location: 'LATAM (UTC-5)' }

function montar(opts: { profile?: typeof PERFIL | null; retained?: boolean } = {}) {
  return render(
    <ToastProvider>
      <CVUpload profile={opts.profile ?? null} retained={opts.retained} onChanged={() => {}} />
    </ToastProvider>,
  )
}

function inputArchivo(): HTMLInputElement {
  return document.querySelector('input[type="file"]') as HTMLInputElement
}

function elegirArchivo() {
  const file = new File(['Ana Perez - Backend Engineer'], 'cv.txt', { type: 'text/plain' })
  fireEvent.change(inputArchivo(), { target: { files: [file] } })
}

let clickSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ profile: PERFIL }),
      text: async () => '',
    }),
  )
  clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => {})
})

afterEach(() => {
  clickSpy.mockRestore()
})

describe('CVUpload — aviso previo (punto 12)', () => {
  it('sin aceptación, Subir muestra el aviso: no abre el selector ni envía nada', () => {
    montar()
    fireEvent.click(screen.getByRole('button', { name: /Subir CV/ }))
    const aviso = screen.getByRole('group', { name: /aviso de envío del CV/i })
    // Qué datos se envían y a qué clase de servicio
    expect(aviso).toHaveTextContent(/nombre/i)
    expect(aviso).toHaveTextContent(/teléfono/i)
    expect(aviso).toHaveTextContent(/historial/i)
    expect(aviso).toHaveTextContent(/proveedor/i)
    expect(aviso).toHaveTextContent(/externo/i)
    expect(clickSpy).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('un archivo que llega sin aceptación no se envía', () => {
    montar()
    elegirArchivo()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('aceptar guarda la versión, abre el selector y el envío lleva la versión', async () => {
    montar()
    fireEvent.click(screen.getByRole('button', { name: /Subir CV/ }))
    fireEvent.click(screen.getByRole('button', { name: /Acepto y elegir archivo/ }))
    expect(localStorage.getItem('gema.cv_llm_consent')).toBe(CV_CONSENT_VERSION)
    expect(clickSpy).toHaveBeenCalledTimes(1)
    elegirArchivo()
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/cv')
    expect((init.body as FormData).get('llm_consent')).toBe(CV_CONSENT_VERSION)
  })

  it('con la aceptación vigente guardada, Subir abre el selector sin repetir el aviso', () => {
    localStorage.setItem('gema.cv_llm_consent', CV_CONSENT_VERSION)
    montar()
    fireEvent.click(screen.getByRole('button', { name: /Subir CV/ }))
    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('group', { name: /aviso de envío del CV/i })).toBeNull()
  })

  it('la aceptación de una versión vieja del aviso no vale', () => {
    localStorage.setItem('gema.cv_llm_consent', 'cv-llm-1999-01-01')
    montar()
    fireEvent.click(screen.getByRole('button', { name: /Subir CV/ }))
    expect(screen.getByRole('group', { name: /aviso de envío del CV/i })).toBeInTheDocument()
    expect(clickSpy).not.toHaveBeenCalled()
  })

  it('Cancelar cierra el aviso sin guardar nada ni enviar', () => {
    montar()
    fireEvent.click(screen.getByRole('button', { name: /Subir CV/ }))
    fireEvent.click(screen.getByRole('button', { name: /Cancelar/ }))
    expect(screen.queryByRole('group', { name: /aviso de envío del CV/i })).toBeNull()
    expect(localStorage.getItem('gema.cv_llm_consent')).toBeNull()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('la aceptación vive aparte de la retención: no toca el scheduler y se puede revocar', () => {
    localStorage.setItem('gema.cv_llm_consent', CV_CONSENT_VERSION)
    montar()
    fireEvent.click(screen.getByRole('button', { name: /Revocar aviso/ }))
    expect(localStorage.getItem('gema.cv_llm_consent')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Subir CV/ }))
    expect(screen.getByRole('group', { name: /aviso de envío del CV/i })).toBeInTheDocument()
    const urls = vi.mocked(fetch).mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/api/scheduler'))).toBe(false)
    expect(Object.keys(localStorage).some((k) => /retain|retener/i.test(k))).toBe(false)
  })

  it('un 413 del servidor muestra su mensaje legible, no el JSON crudo', async () => {
    localStorage.setItem('gema.cv_llm_consent', CV_CONSENT_VERSION)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 413,
        json: async () => ({ detail: 'El archivo supera el límite de 5 MB para un CV.' }),
        text: async () => JSON.stringify({ detail: 'El archivo supera el límite de 5 MB para un CV.' }),
      }),
    )
    montar()
    elegirArchivo()
    expect(await screen.findByText(/supera el límite de 5 MB/)).toBeInTheDocument()
    expect(screen.queryByText(/\{"detail"/)).toBeNull()
  })
})

describe('CVUpload — controles de perfil que dicen la verdad (punto 11)', () => {
  it('Descartar perfil llama DELETE /api/cv', async () => {
    montar({ profile: PERFIL })
    fireEvent.click(screen.getByRole('button', { name: /Descartar perfil/ }))
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/cv')
    expect(init.method).toBe('DELETE')
  })

  it('con perfil retenido lo dice, y Descartar avisa que borra también el archivo', () => {
    montar({ profile: PERFIL, retained: true })
    expect(screen.getByText(/retenido en disco/i)).toBeInTheDocument()
  })

  it('sin retención, la nota "nada se guarda" sigue siendo verdad', () => {
    montar({ profile: null, retained: false })
    expect(screen.getByText(/Nada se guarda más allá de la sesión/)).toBeInTheDocument()
  })
})

const SCHED: SchedulerState = {
  enabled: true,
  armed: false,
  mode: 'interval',
  interval_hours: 4,
  cron_days: ['tue'],
  cron_hour: 9,
  cron_minute: 0,
  is_running: false,
  next_run: 'mar 09:00',
  last_run: 'No runs yet',
  has_config: true,
  retain_cv: true,
}

describe('SchedulerPanel — la retención refleja al servidor (punto 11)', () => {
  function toggleRetener(): HTMLElement {
    const fila = screen.getByText('Retener CV entre corridas').parentElement as HTMLElement
    return fila.querySelector('[role="switch"]') as HTMLElement
  }

  it('el toggle nace con la preferencia real, en cron e intervalo', () => {
    render(
      <ToastProvider>
        <SchedulerPanel scheduler={SCHED} onChanged={() => {}} />
      </ToastProvider>,
    )
    expect(toggleRetener()).toHaveAttribute('aria-checked', 'true')
  })

  it('apagar el cron conservando la retención envía retain_cv=true (no lo pisa con false)', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true, status: 200, json: async () => SCHED, text: async () => '',
    } as Response)
    render(
      <ToastProvider>
        <SchedulerPanel scheduler={SCHED} onChanged={() => {}} />
      </ToastProvider>,
    )
    // El primer switch del panel es "Scraping automático"
    fireEvent.click(screen.getAllByRole('switch')[0])
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    const init = vi.mocked(fetch).mock.calls[0][1] as RequestInit
    expect(JSON.parse(init.body as string).retain_cv).toBe(true)
  })

  it('con el cron apagado, cambiar la retención igual llega al servidor', async () => {
    // Antes solo se mandaba con el cron prendido: la nota prometía "se guarda en disco"
    // y el servidor seguía con retain_cv=false para el próximo CV.
    const apagado: SchedulerState = { ...SCHED, enabled: false, armed: false, retain_cv: false }
    vi.mocked(fetch).mockResolvedValue({
      ok: true, status: 200, json: async () => ({ ...apagado, retain_cv: true }),
      text: async () => '',
    } as Response)
    render(
      <ToastProvider>
        <SchedulerPanel scheduler={apagado} onChanged={() => {}} />
      </ToastProvider>,
    )
    fireEvent.click(toggleRetener())
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    const body = JSON.parse((vi.mocked(fetch).mock.calls[0][1] as RequestInit).body as string)
    expect(body.enabled).toBe(false)
    expect(body.retain_cv).toBe(true)
  })
})
