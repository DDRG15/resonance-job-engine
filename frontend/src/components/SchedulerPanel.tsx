import { useState } from 'react'
import { putScheduler } from '../api'
import type { SchedulerState } from '../types'

const DAYS: { key: string; label: string }[] = [
  { key: 'mon', label: 'L' }, { key: 'tue', label: 'M' }, { key: 'wed', label: 'X' },
  { key: 'thu', label: 'J' }, { key: 'fri', label: 'V' }, { key: 'sat', label: 'S' },
  { key: 'sun', label: 'D' },
]
const INTERVALS = [2, 4, 8, 12, 24]

interface Props {
  scheduler: SchedulerState
  hasConfig: boolean
  onChanged: () => void
}

// Cron B4 completo — NO-NEGOCIABLE: días + hora + retención de CV,
// auto-configurable desde la UI. Cada interacción hace PUT /api/scheduler
// (misma semántica apply-on-change que tenía Streamlit).
export default function SchedulerPanel({ scheduler, hasConfig, onChanged }: Props) {
  const [enabled, setEnabled] = useState(scheduler.enabled)
  const [mode, setMode] = useState<'interval' | 'cron'>(scheduler.mode)
  const [intervalHours, setIntervalHours] = useState(scheduler.interval_hours)
  const [days, setDays] = useState<string[]>(scheduler.cron_days)
  const [hour, setHour] = useState(scheduler.cron_hour)
  const [retainCv, setRetainCv] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Patrón "adjusting state when a prop changes" — el server confirma el
  // estado real tras cada PUT y este bloque re-sincroniza sin useEffect.
  const [lastScheduler, setLastScheduler] = useState(scheduler)
  if (scheduler !== lastScheduler) {
    setLastScheduler(scheduler)
    setEnabled(scheduler.enabled)
    setMode(scheduler.mode)
    setIntervalHours(scheduler.interval_hours)
    setDays(scheduler.cron_days)
    setHour(scheduler.cron_hour)
  }

  async function apply(next: {
    enabled?: boolean; mode?: 'interval' | 'cron'; interval_hours?: number
    cron_days?: string[]; cron_hour?: number; retain_cv?: boolean
  }) {
    setError(null)
    try {
      await putScheduler({
        enabled: next.enabled ?? enabled,
        mode: next.mode ?? mode,
        interval_hours: next.interval_hours ?? intervalHours,
        cron_days: next.cron_days ?? days,
        cron_hour: next.cron_hour ?? hour,
        retain_cv: next.retain_cv ?? retainCv,
      })
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const disabled = !hasConfig

  function toggleDay(key: string) {
    const next = days.includes(key) ? days.filter((d) => d !== key) : [...days, key]
    setDays(next)
    if (enabled) void apply({ cron_days: next })
  }

  return (
    <div className="side-sec">
      <span className="side-label">Auto-Scheduler</span>

      <div className="toggle-row">
        <span>Scraping automático</span>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          className={`switch ${enabled ? 'on' : ''}`}
          disabled={disabled}
          onClick={() => {
            const next = !enabled
            setEnabled(next)
            void apply({ enabled: next })
          }}
        />
      </div>

      <div className="seg">
        <button
          type="button"
          className={mode === 'interval' ? 'on' : ''}
          disabled={disabled}
          onClick={() => {
            setMode('interval')
            if (enabled) void apply({ mode: 'interval' })
          }}
        >
          Interval
        </button>
        <button
          type="button"
          className={mode === 'cron' ? 'on' : ''}
          disabled={disabled}
          onClick={() => {
            setMode('cron')
            if (enabled) void apply({ mode: 'cron' })
          }}
        >
          Cron (días fijos)
        </button>
      </div>

      {mode === 'interval' ? (
        <div className="seg">
          {INTERVALS.map((h) => (
            <button
              type="button"
              key={h}
              className={intervalHours === h ? 'on' : ''}
              disabled={disabled}
              onClick={() => {
                setIntervalHours(h)
                if (enabled) void apply({ interval_hours: h })
              }}
            >
              {h}h
            </button>
          ))}
        </div>
      ) : (
        <>
          <div className="days">
            {DAYS.map((d) => (
              <button
                type="button"
                key={d.key}
                className={`day ${days.includes(d.key) ? 'on' : ''}`}
                disabled={disabled}
                onClick={() => toggleDay(d.key)}
              >
                {d.label}
              </button>
            ))}
          </div>
          <input
            type="range"
            className="slider"
            min={0}
            max={23}
            value={hour}
            disabled={disabled}
            onChange={(e) => setHour(Number(e.target.value))}
            onMouseUp={() => enabled && void apply({ cron_hour: hour })}
            onTouchEnd={() => enabled && void apply({ cron_hour: hour })}
          />
          <span className="slider-val">
            {String(hour).padStart(2, '0')}:00 — hora local (la ciudad del candidato)
          </span>
          <div className="toggle-row">
            <span>Retener CV entre corridas</span>
            <button
              type="button"
              role="switch"
              aria-checked={retainCv}
              className={`switch ${retainCv ? 'on' : ''}`}
              disabled={disabled}
              onClick={() => {
                const next = !retainCv
                setRetainCv(next)
                if (enabled) void apply({ retain_cv: next })
              }}
            />
          </div>
        </>
      )}

      {scheduler.enabled ? (
        <div className="sched-status">
          ● Activo — próxima corrida: {scheduler.next_run}
          <small>Última: {scheduler.last_run}</small>
        </div>
      ) : (
        <span className="side-note">
          {hasConfig
            ? '○ Pausado'
            : 'Confirmá una búsqueda para habilitar el scheduler.'}
        </span>
      )}
      {error && <span className="side-note" style={{ color: 'var(--t3-ink)' }}>{error}</span>}
    </div>
  )
}
