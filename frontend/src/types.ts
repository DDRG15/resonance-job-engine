// Tipos espejo del contrato API (api.py — B6.0, PHASE_FIX_)

export interface Status {
  boards: number
  boards_active: number
  stale_selectors: string[]
  providers_configured: number
  warnings: string[]
  nominal: boolean
  scheduler: { enabled: boolean; next_run: string | null }
  is_running: boolean
  has_results: boolean
  needs_setup: boolean
  ttl_hours: number
  integrations: { notion: boolean; sheets: boolean }
}

export interface RegistryStats {
  total_seen: number
  recent_24h: number
  by_tier: Record<string, number>
}

export interface SearchConfigDto {
  target_titles: string[]
  must_include: string[]
  must_exclude: string[]
  min_salary: number | null
  target_domains: string[]
  custom_xpath: string | null
  custom_css: string | null
}

export type AuditVerdict = 'clean' | 'issues' | 'skipped' | 'unavailable'

export interface ParseResponse {
  config: SearchConfigDto
  audit_report: string
  audit_verdict: AuditVerdict
}

export interface VaultEntry {
  vault_id: string
  label: string
  config: SearchConfigDto
  created_at: string
  times_used: number
}

export interface SchedulerState {
  enabled: boolean
  armed: boolean
  mode: 'interval' | 'cron'
  interval_hours: number
  cron_days: string[]
  cron_hour: number
  cron_minute: number
  is_running: boolean
  next_run: string | null
  last_run: string
  has_config: boolean
}

export interface SchedulerUpdate {
  enabled: boolean
  mode: 'interval' | 'cron'
  interval_hours: number
  cron_days: string[]
  cron_hour: number
  cron_minute: number
  retain_cv: boolean
}

export interface JobDto {
  title: string
  company: string
  url: string
  salary_raw: string | null
  location_raw: string | null
  source_domain: string
  scraped_at: string
}

export interface TieredJobDto {
  job: JobDto
  match_score: number
  tier: string
  match_reasons: string[]
  miss_reasons: string[]
  // Fase 3: la elegibilidad sale del score. El backend los manda siempre
  // (models.py les da default PASS/""), asi que no son opcionales: declararlos
  // opcionales invitaria a no mostrarlos, que es como estuvieron hasta hoy.
  location_verdict: string
  location_note: string
  language_verdict: string
  language_note: string
}

export interface SummaryDto {
  run_id: string
  total_urls_found: number
  skipped_seen: number
  skipped_ttl: number
  new_processed: number
  tier1_count: number
  tier2_count: number
  tier3_count: number
  domains_aborted: string[]
  errors: string[]
  duration_seconds: number | null
}

export interface ResultsResponse {
  run_id: string | null
  is_running: boolean
  has_results: boolean
  summary: SummaryDto | null
  tiers: {
    tier1: TieredJobDto[]
    tier2: TieredJobDto[]
    tier3: TieredJobDto[]
    tier4: TieredJobDto[]
  } | null
}

export interface RegistryJob {
  job_hash: string
  url: string
  title: string
  company: string
  source_domain: string
  scraped_at: string
  tier: string | null
  match_score: number | null
  location_raw: string | null
  salary_raw: string | null
  // Fase 5: el razonamiento dejo de morir con la sesion. Opcionales porque las
  // filas anteriores al 2026-09-16 no los tienen y nunca los van a tener —
  // se guardan al puntuar, y esas ya se puntuaron.
  match_reasons: string | null
  miss_reasons: string | null
  location_verdict: string | null
  location_note: string | null
  language_verdict: string | null
  language_note: string | null
  // Un score v1 y uno v2 NO son comparables: la Fase 3 saco la ubicacion del
  // puntaje y la Fase 4 renormalizo las dimensiones. Mostrar la version al lado
  // del numero es lo que evita compararlos sin darse cuenta.
  score_version: string | null
}

export interface ReviewJob {
  review_hash: string
  title: string
  company: string
  source_domain: string
  original_url: string | null
  srp_url: string | null
  reason: string | null
  captured_at: string
  resolved: number
  resolved_url: string | null
}

// Fase 7 — el ciclo de vida de una postulacion.
export interface Application {
  app_id: string
  job_hash: string
  url: string
  title: string | null
  company: string | null
  source_domain: string | null
  // Copiados AL APLICAR, no leidos despues: el matcher cambia (las Fases 3 y 4 ya
  // lo cambiaron dos veces) y la pregunta es si el score que viste predijo algo.
  tier_al_aplicar: string | null
  score_al_aplicar: number | null
  status: string
  created_at: string
  stage_changed_at: string
  applied_at: string | null
  closed_at: string | null
  salary_offered: number | null
  follow_up_at: string | null
  notes: string | null
}

export interface ApplicationsResponse {
  applications: Application[]
  // La maquina de estados viaja con la respuesta para que el front no la duplique:
  // si vive en dos lugares, el dia que se agregue un estado se actualiza uno solo.
  estados: string[]
  cadena: string[]
  terminales: string[]
}

export interface CvProfile {
  [key: string]: unknown
  role?: string
  core_skills?: string[]
  location?: string
}

// Fase 11 — pares de avisos que el correlator no se anima a decidir solo.
export interface CorrelatorPendiente {
  job_hash: string
  hash_duplicado: string
  correlator: string
  confianza: number
  motivo: string | null
  creado_en: string | null
  can_title: string | null
  can_company: string | null
  can_board: string | null
  can_fecha: string | null
  can_url: string | null
  can_ubicacion: string | null
  dup_title: string | null
  dup_company: string | null
  dup_board: string | null
  dup_fecha: string | null
  dup_url: string | null
  dup_ubicacion: string | null
}

export interface CorrelatorsResponse {
  pendientes: CorrelatorPendiente[]
  total: number
}
