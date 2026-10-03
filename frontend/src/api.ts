// Cliente HTTP fino contra api.py. En dev, Vite proxya /api → localhost:8000.

import type {
  AuditVerdict, CvProfile, FuentesResponse, ParseResponse, RegistryStats, ResultsResponse, SchedulerState,
  SchedulerUpdate, SearchConfigDto, Status, VaultEntry,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`HTTP ${res.status} — ${body}`)
  }
  return res.json() as Promise<T>
}

export const getStatus = () => request<Status>('/api/status')

export const getFuentes = () => request<FuentesResponse>('/api/fuentes')

export const getRegistryStats = () => request<RegistryStats>('/api/registry/stats')

export const getVault = () => request<{ entries: VaultEntry[] }>('/api/vault')

export const getScheduler = () => request<SchedulerState>('/api/scheduler')

export const postParse = (prompt: string, vaultLabel?: string) =>
  request<ParseResponse>('/api/parse', {
    method: 'POST',
    body: JSON.stringify({ prompt, vault_label: vaultLabel ?? null }),
  })

export const loadVaultEntry = (vaultId: string) =>
  request<{ config: SearchConfigDto; label: string }>(
    `/api/vault/${vaultId}/load`, { method: 'POST' },
  )

export const putConfig = (config: unknown) =>
  request<{ config: SearchConfigDto }>('/api/config', {
    method: 'PUT',
    body: JSON.stringify(config),
  })

export const postScrape = (ttlHours: number) =>
  request<{ run_id: string }>('/api/scrape', {
    method: 'POST',
    body: JSON.stringify({ ttl_hours: ttlHours }),
  })

export const getResults = () => request<ResultsResponse>('/api/results')

// La búsqueda confirmada que el servidor ya tiene. Existía GET /api/config y el front
// nunca lo pedía: un refresh perdía la búsqueda aunque el server la conservara.
export const getConfig = () =>
  request<{ config: SearchConfigDto | null; audit_report: string | null;
            audit_verdict: AuditVerdict | null }>('/api/config')

export const putScheduler = (update: SchedulerUpdate) =>
  request<SchedulerState>('/api/scheduler', {
    method: 'PUT',
    body: JSON.stringify(update),
  })

export const putGodmode = (customXpath: string, customCss: string) =>
  request<{ godmode: { custom_xpath: string | null; custom_css: string | null } }>(
    '/api/godmode', {
      method: 'PUT',
      body: JSON.stringify({
        custom_xpath: customXpath || null,
        custom_css: customCss || null,
      }),
    },
  )

export const postReset = () =>
  request<{ reset: boolean }>('/api/reset', { method: 'POST' })

export const getCvProfile = () =>
  request<{ profile: CvProfile | null }>('/api/cv')

export async function postCv(file: File): Promise<{ profile: CvProfile }> {
  const form = new FormData()
  form.append('file', file)
  // Sin Content-Type manual: el browser setea el boundary de multipart
  const res = await fetch('/api/cv', { method: 'POST', body: form })
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${await res.text()}`)
  return res.json()
}

export const deleteCv = () =>
  request<{ profile: null }>('/api/cv', { method: 'DELETE' })
