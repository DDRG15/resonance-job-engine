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
  onOpenPipeline: () => void
  onOpenDuplicados: () => void
  onOpenFuentes: () => void
  // Fuentes SOSPECHOSAS (cayeron a cero contra su historial); 0 si no hay o no se midió.
  fuentesSospechosas: number
  duplicadosPending: number
  reviewPending: number
  // Fase 9: cuántas postulaciones abiertas tienen la próxima acción atrasada. Opcional
  // para que quien monte el Sidebar sin esa consulta no vea un badge inventado.
  overdueApps?: number
}

export default function Sidebar({
  status, stats, vault, scheduler, profile,
  ttlHours, onTtlChange, onLoadVault, onChanged, onKill, onOpenDb, onOpenStats,
  onOpenReview,
  onOpenPipeline, reviewPending, onOpenDuplicados, duplicadosPending,
  onOpenFuentes, fuentesSospechosas,
  overdueApps = 0,
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

  // revision externa 2026-09-30, punto 1: el botón se llamaba "Kill Switch" y nunca mató nada —
  // una corrida en curso no se puede cancelar. Mientras haya una (manual o del cron) el
  // backend responde 409, así que el botón se deshabilita y dice por qué en vez de
  // dejar que el clic falle.
  const corriendo = !!status && (status.is_running || !!status.scrape_gate_busy)
  // Sin status todavía no se sabe si hay corrida: se espera en vez de adivinar.
  const bloqueado = !status || corriendo

  async function handleKill() {
    if (bloqueado) return
    if (!killArmed) {
      setKillArmed(true)
      return
    }
    setKillArmed(false)
    try {
      await postReset()
    } catch (e) {
      // 409: la corrida arrancó entre que se pintó el botón y el clic.
      const msg = String(e)
      toast(
        msg.includes('409')
          ? 'No se puede limpiar la sesión: hay una corrida activa. Esperá a que termine.'
          : `No se pudo limpiar la sesión — ${msg}`,
        'error',
      )
      return
    }
    toast('Sesión limpiada — config, perfil y cron borrados', 'warn')
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

      {/* Fase 4: el token de Notion se valida al arrancar la API. Antes la tarjeta
          mostraba la integración activa con solo que la key existiera. */}
      {status?.notion_estado && status.notion_estado.estado !== 'apagado' && (
        <span
          className={status.notion_estado.estado === 'valido' ? 'pill-ok'
            : status.notion_estado.estado === 'invalido' ? 'pill-bad' : 'pill-warn'}
          title={status.notion_estado.detalle}
          data-testid="notion-estado"
        >
          <span className="dot" />{' '}
          {status.notion_estado.estado === 'valido' ? 'Notion · token válido'
            : status.notion_estado.estado === 'invalido' ? 'Notion · token inválido'
            : 'Notion · no verificado'}
        </span>
      )}

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
          <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
            <button type="button" className="btn mini" onClick={onOpenReview}
                    style={{ flex: 1 }}>
              Revisión{reviewPending > 0 ? ` (${reviewPending})` : ''}
            </button>
            <button type="button" className="btn mini" onClick={onOpenPipeline}
                    style={{ flex: 1 }}>
              Postulaciones
              {overdueApps > 0 && (
                <span
                  className="side-badge"
                  title={`${overdueApps} próxima(s) acción(es) vencida(s)`}
                  aria-label={`${overdueApps} ${overdueApps === 1 ? 'vencida' : 'vencidas'}`}
                >
                  {overdueApps}
                </span>
              )}
            </button>
          </div>
          <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
            <button type="button" className="btn mini" onClick={onOpenDuplicados}
                    style={{ flex: 1 }}>
              Duplicados{duplicadosPending > 0 ? ` (${duplicadosPending})` : ''}
            </button>
            <button type="button" className="btn mini" onClick={onOpenFuentes}
                    style={{ flex: 1 }}>
              Fuentes{fuentesSospechosas > 0 ? ` (${fuentesSospechosas})` : ''}
            </button>
          </div>
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

      {corriendo && (
        <span className="side-note" style={{ marginTop: 'auto' }}>
          Hay una corrida en curso y no se puede cancelar: la sesión se puede limpiar
          cuando termine.
        </span>
      )}
      <button
        type="button"
        className="kill"
        disabled={bloqueado}
        title={corriendo ? 'Hay una corrida en curso; no se puede limpiar hasta que termine' : undefined}
        style={{
          ...(killArmed ? { background: 'rgba(220,38,38,.25)', fontWeight: 700 } : {}),
          ...(corriendo ? { marginTop: 6 } : {}),
        }}
        onClick={() => void handleKill()}
      >
        {killArmed
          ? '¿Seguro? Borra sesión y apaga el cron — clic para confirmar'
          : 'Limpiar sesión'}
      </button>
    </aside>
  )
}
