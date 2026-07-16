import { useEffect, useState } from 'react'
import { postReset } from '../api'
import { useToast } from '../toast'
import type { CvProfile, RegistryStats, SchedulerState, Status, VaultEntry } from '../types'
import CVUpload from './CVUpload'
import GodMode from './GodMode'
import SchedulerPanel from './SchedulerPanel'

interface Props {
  status: Status | null
  stats: RegistryStats | null
  vault: VaultEntry[]
  scheduler: SchedulerState | null
  profile: CvProfile | null
  ttlHours: number
  onTtlChange: (hours: number) => void
  onLoadVault: (vaultId: string) => void
  onChanged: () => void
  onKill: () => void
  onOpenDb: () => void
  onOpenStats: () => void
  onOpenReview: () => void
  reviewPending: number
}

export default function Sidebar({
  status, stats, vault, scheduler, profile,
  ttlHours, onTtlChange, onLoadVault, onChanged, onKill, onOpenDb, onOpenStats,
  onOpenReview, reviewPending,
}: Props) {
  // Triaje Kimi r3: confirmación SOLO para destructivas — y el kill switch
  // es LA destructiva (borra sesión, apaga cron, limpia estado persistido F8).
  // Doble paso: primer clic arma, segundo confirma; se desarma solo en 4 s.
  const [killArmed, setKillArmed] = useState(false)
  const toast = useToast()
  useEffect(() => {
    if (!killArmed) return
    const t = setTimeout(() => setKillArmed(false), 4000)
    return () => clearTimeout(t)
  }, [killArmed])

  async function handleKill() {
    if (!killArmed) {
      setKillArmed(true)
      return
    }
    setKillArmed(false)
    await postReset()
    toast('Sesión reseteada — config, perfil y cron limpiados', 'warn')
    onKill()
  }

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-gem">◆</div>
        <h1>
          GEMA<small>Control Panel</small>
        </h1>
      </div>

      {status &&
        (status.warnings.length === 0 ? (
          <span className="pill-ok">
            <span className="dot" /> All systems nominal
          </span>
        ) : (
          <details className="warn-details">
            <summary>
              <span className="pill-warn">
                <span className="dot" /> {status.warnings.length} warning(s) — ver detalle
              </span>
            </summary>
            <div className="warn-list">
              {status.warnings.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
          </details>
        ))}

      <CVUpload profile={profile} onChanged={onChanged} />

      {stats && (
        <div className="side-sec">
          <span className="side-label">Seen Registry</span>
          <div className="mini-stats">
            <div className="mini-stat">
              <b>{stats.total_seen.toLocaleString('es-PE')}</b>
              <span>Total seen</span>
            </div>
            <div className="mini-stat">
              <b>{stats.recent_24h.toLocaleString('es-PE')}</b>
              <span>Últimas 24 h</span>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button type="button" className="btn mini" onClick={onOpenDb} style={{ flex: 1 }}>
              Ver DB
            </button>
            <button type="button" className="btn mini" onClick={onOpenStats} style={{ flex: 1 }}>
              Estadísticas
            </button>
          </div>
          <button type="button" className="btn mini" onClick={onOpenReview}>
            Revisión manual{reviewPending > 0 ? ` (${reviewPending})` : ''}
          </button>
        </div>
      )}

      <div className="side-sec">
        <span className="side-label">Search Vault</span>
        {vault.length === 0 && (
          <span className="side-note">
            Vacío — guardá una búsqueda desde el Command Center.
          </span>
        )}
        {vault.map((e) => (
          <div className="vault-entry" key={e.vault_id}>
            <div>
              {e.label}
              <small>usada {e.times_used}×</small>
            </div>
            <button className="btn mini" onClick={() => onLoadVault(e.vault_id)}>
              Cargar
            </button>
          </div>
        ))}
      </div>

      <div className="side-sec">
        <span className="side-label">Delta Load — TTL</span>
        <input
          type="range"
          className="slider"
          min={0}
          max={168}
          value={ttlHours}
          onChange={(e) => onTtlChange(Number(e.target.value))}
        />
        <span className="slider-val">
          {ttlHours === 0
            ? '0 h — nunca re-procesar jobs ya vistos'
            : `${ttlHours} h — re-procesa jobs vistos hace más de ${ttlHours} h`}
        </span>
      </div>

      {scheduler && <SchedulerPanel scheduler={scheduler} onChanged={onChanged} />}

      <GodMode />

      <button
        type="button"
        className="kill"
        style={killArmed ? { background: 'rgba(220,38,38,.25)', fontWeight: 700 } : undefined}
        onClick={() => void handleKill()}
      >
        {killArmed
          ? '¿Seguro? Borra sesión y apaga el cron — clic para confirmar'
          : 'Reset Session — Kill Switch'}
      </button>
    </aside>
  )
}
