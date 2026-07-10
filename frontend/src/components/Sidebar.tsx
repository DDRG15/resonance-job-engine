import { postReset } from '../api'
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
}

export default function Sidebar({
  status, stats, vault, scheduler, profile,
  ttlHours, onTtlChange, onLoadVault, onChanged, onKill, onOpenDb,
}: Props) {
  async function handleKill() {
    await postReset()
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
          <button type="button" className="btn mini" onClick={onOpenDb}>
            Ver base de datos
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

      <button type="button" className="kill" onClick={() => void handleKill()}>
        Reset Session — Kill Switch
      </button>
    </aside>
  )
}
