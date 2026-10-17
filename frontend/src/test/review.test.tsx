import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import ReviewPanel from '../components/ReviewPanel'
import type { ReviewJob } from '../types'

const JOB: ReviewJob = {
  review_hash: 'review-1', title: 'Python Developer', company: '(empresa no listada)',
  source_domain: 'example.com', original_url: 'https://example.com/jobs/1',
  srp_url: 'https://example.com/jobs', resolved_url: null,
  reason: 'empresa_dudosa_sin_empresa', captured_at: '2026-10-17T12:00:00Z',
  resolved: 0,
}

function preparar(job: ReviewJob) {
  const fetchMock = vi.fn().mockImplementation(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') return { ok: true, json: async () => ({ resolved: true }) }
    return { ok: true, json: async () => ({ jobs: [job], stats: { pending: 1, resolved: 0 } }) }
  })
  vi.stubGlobal('fetch', fetchMock)
  const onChanged = vi.fn()
  render(<ReviewPanel onClose={() => {}} onChanged={onChanged} />)
  return { fetchMock, onChanged }
}

describe('ReviewPanel', () => {
  it('marca una empresa dudosa como revisada sin pedir otra URL', async () => {
    const { fetchMock, onChanged } = preparar(JOB)
    const button = await screen.findByRole('button', { name: 'Marcar como revisado' })
    expect(screen.getByRole('link', { name: /Abrir el aviso/ })).toHaveAttribute('href', JOB.original_url)
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Descartar' })).toBeNull()
    fireEvent.click(button)
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce())
    expect(fetchMock).toHaveBeenCalledWith('/api/review/review-1/resolve', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ resolved_url: null }),
    }))
  })

  it('conserva el flujo de corregir URLs para los avisos rescatados', async () => {
    const { fetchMock } = preparar({ ...JOB, reason: 'url_no_verificable_titulo_real' })
    const input = await screen.findByRole('textbox')
    const button = screen.getByRole('button', { name: 'Guardar URL' })
    expect(button).toBeDisabled()
    fireEvent.change(input, { target: { value: 'https://example.com/jobs/real' } })
    fireEvent.click(button)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/review/review-1/resolve', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ resolved_url: 'https://example.com/jobs/real' }),
    })))
  })

  it('no convierte URLs de otro esquema en enlaces', async () => {
    preparar({ ...JOB, original_url: 'javascript:alert(1)', srp_url: 'file:///secret' })
    await screen.findByRole('button', { name: 'Marcar como revisado' })
    expect(screen.queryByRole('link')).toBeNull()
  })
})
