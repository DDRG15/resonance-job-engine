import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import LastRunBanner from '../components/LastRunBanner'
import type { LastRunDto } from '../types'

const run: LastRunDto = {
  run_id: 'saved-run', status: 'interrupted', trigger: 'api', started_at: null,
  completed_at: null, interrupted_at: null, error: null, tier_counts: null,
  results_available: false, resumable: true, message: 'Podés reanudar esta corrida.',
}

describe('Reanudar corrida', () => {
  it('continúa el identificador guardado y evita dos clics mientras espera', async () => {
    let finish: () => void = () => {}
    const resume = vi.fn(() => new Promise<void>((resolve) => { finish = resolve }))
    render(<LastRunBanner lastRun={run} onResume={resume} />)
    fireEvent.click(screen.getByRole('button', { name: 'Reanudar corrida' }))
    expect(resume).toHaveBeenCalledWith('saved-run')
    expect(screen.getByRole('button', { name: 'Reanudando…' })).toBeDisabled()
    finish()
    await waitFor(() => expect(screen.getByRole('button')).toBeEnabled())
  })

  it('muestra el error del servidor y permite reintentar', async () => {
    const resume = vi.fn().mockRejectedValue(new Error('Ya hay una corrida activa.'))
    render(<LastRunBanner lastRun={run} onResume={resume} />)
    fireEvent.click(screen.getByRole('button'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Ya hay una corrida activa.')
    expect(screen.getByRole('button')).toBeEnabled()
  })

  it('espera el CV necesario antes de reanudar', () => {
    render(<LastRunBanner lastRun={{ ...run, resume_requires_cv: true }} onResume={vi.fn()} />)
    expect(screen.getByRole('button')).toBeDisabled()
  })

  it('oculta el control cuando no existe un avance recuperable', () => {
    render(<LastRunBanner lastRun={{ ...run, resumable: false }} onResume={vi.fn()} />)
    expect(screen.queryByRole('button')).toBeNull()
  })
})
