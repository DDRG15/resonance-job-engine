import { useState } from 'react'
import { putScheduler } from '../api'
import { useToast } from '../toast'
import type { SchedulerState } from '../types'

const DAYS: { key: string; label: string }[] = [
  { key: 'mon', label: 'L' }, { key: 'tue', label: 'M' }, { key: 'wed', label: 'X' },
  { key: 'thu', label: 'J' }, { key: 'fri', label: 'V' }, { key: 'sat', label: 'S' },
  { key: 'sun', label: 'D' },
]
const INTERVALS = [2, 4, 8, 12, 24]

interface Props {
  scheduler: SchedulerState
  onChanged: () => void
}

// Cron B4 completo — NO-NEGOCIABLE: días + hora + retención de CV,
// auto-configurable desde la UI EN CUALQUIER MOMENTO: sin búsqueda confirmada el horario queda "armado" en el
// backend y se activa solo al confirmar. Apply-on-change en cada interacción.
export default function SchedulerPanel({ scheduler, onChanged }: Props) {
  const [enabled, setEnabled] = useState(scheduler.enabled || scheduler.armed)
  const [mode, setMode] = useState<'interval' | 'cron'>(scheduler.mode)
  const [intervalHours, setIntervalHours] = useState(scheduler.interval_hours)
  const [days, setDays] = useState<string[]>(scheduler.cron_days)
  const [hour, setHour] = useState(scheduler.cron_hour)
  const [minute, setMinute] = useState(scheduler.cron_minute)
  // La preferencia viene del servidor. Nacía en false en cada carga y el
  // primer PUT siguiente (p. ej. apagar el cron) la enviaba así, pidiendo borrar el perfil.
  const [retainCv, setRetainCv] = useState(scheduler.retain_cv)
  const [error, setError] = useState<string | null>(null)

  // Patrón "adjusting state when a prop changes" — el server confirma el
  // estado real tras cada PUT y este bloque re-sincroniza sin useEffect.
  const [lastScheduler, setLastScheduler] = useState(scheduler)
  if (scheduler !== lastScheduler) {
    setLastScheduler(scheduler)
    setEnabled(scheduler.enabled || scheduler.armed)
    setMode(scheduler.mode)
    setIntervalHours(scheduler.interval_hours)
    setDays(scheduler.cron_days)
    setHour(scheduler.cron_hour)
    setMinute(scheduler.cron_minute)
    setRetainCv(scheduler.retain_cv)
  }

  const toast = useToast()

  async function apply(next: {
    enabled?: boolean; mode?: 'interval' | 'cron'; interval_hours?: number
    cron_days?: string[]; cron_hour?: number; cron_minute?: number
    retain_cv?: boolean
  }) {
    setError(null)
    try {
      const state = await putScheduler({
        enabled: next.enabled ?? enabled,
        mode: next.mode ?? mode,
        interval_hours: next.interval_hours ?? intervalHours,
        cron_days: next.cron_days ?? days,
        cron_hour: next.cron_hour ?? hour,
        cron_minute: next.cron_minute ?? minute,
        retain_cv: next.retain_cv ?? retainCv,
      })
      if (state.enabled) {
        toast(`Cron activo — próxima corrida: ${state.next_run}`, 'success')
      } else if (state.armed) {
        toast('Cron programado — se activa al confirmar tu búsqueda', 'info')
      }
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

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
                onClick={() => toggleDay(d.key)}
              >
                {d.label}
              </button>
            ))}
          </div>
          <input
            type="time"
            className="time-input"
            value={`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`}
            onChange={(e) => {
              const [h, m] = e.target.value.split(':').map(Number)
              if (Number.isNaN(h) || Number.isNaN(m)) return
              setHour(h)
              setMinute(m)
              if (enabled) void apply({ cron_hour: h, cron_minute: m })
            }}
          />
          <span className="slider-val">
            {String(hour).padStart(2, '0')}:{String(minute).padStart(2, '0')} — hora
            configurada, precisión de minutos
          </span>
        </>
      )}

      {/* La retención aplica a ambos modos (el backend la guarda en el scheduler, no en el
          modo): ocultarla en intervalo dejaba una preferencia activa sin control visible. */}
      <div className="toggle-row">
        <span>Retener CV entre corridas</span>
        <button
          type="button"
          role="switch"
          aria-checked={retainCv}
          className={`switch ${retainCv ? 'on' : ''}`}
          onClick={() => {
            const next = !retainCv
            setRetainCv(next)
            // Siempre al servidor, con o sin cron: la preferencia rige el próximo CV que se
            // suba. Con el cron apagado el PUT va con enabled=false y el backend la guarda
            // («pausar, no descartar»). Antes solo se mandaba con el cron prendido y la
            // nota de abajo prometía una retención que el servidor no conocía.
            void apply({ retain_cv: next })
          }}
        />
      </div>
      <span className="side-note">
        {retainCv
          ? 'Se guarda en disco solo el perfil (rol, skills, región), nunca el CV. Apagar esto borra ese archivo del disco.'
          : 'El perfil vive solo en memoria y se pierde al reiniciar el server.'}
      </span>

      {scheduler.enabled ? (
        <div className="sched-status">
          ● Activo — próxima corrida: {scheduler.next_run}
          <small>Última: {scheduler.last_run}</small>
        </div>
      ) : scheduler.armed ? (
        <div className="sched-status" style={{ color: 'var(--t2-ink)' }}>
          ● Programado — se activa al confirmar tu búsqueda
          <small>Última: {scheduler.last_run}</small>
        </div>
      ) : (
        <span className="side-note">
          ○ Pausado — programalo cuando quieras; empieza a correr apenas
          confirmes una búsqueda.
        </span>
      )}
      {error && <span className="side-note" style={{ color: 'var(--t3-ink)' }}>{error}</span>}
    </div>
  )
}
