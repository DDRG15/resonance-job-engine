// Tipos espejo del contrato API (api.py — B6.0, PHASE_FIX_)

export interface Status {
  boards: number
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
}

export interface CvProfile {
  [key: string]: unknown
  role?: string
  core_skills?: string[]
  location?: string
}
