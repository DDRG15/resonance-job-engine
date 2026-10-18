import type { LastRunDto } from '../types'
import { useState } from 'react'

/**
 * Qué se sabe del último run cuando la sesión no tiene resultados. Tras reiniciar el servidor la memoria queda vacía; la fila de `runs` en la
 * DB sobrevive y dice cómo terminó la corrida. El mensaje lo arma el backend para que
 * esta pantalla no tenga que adivinar qué quedó disponible y qué no.
 */
export default function LastRunBanner({ lastRun, onResume }: {
  lastRun: LastRunDto; onResume?: (runId: string) => Promise<void>
}) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function resume() {
    if (!onResume || pending) return
    setPending(true)
    setError(null)
    try { await onResume(lastRun.run_id) }
    catch (e) { setError(e instanceof Error ? e.message : 'No se pudo reanudar.') }
    finally { setPending(false) }
  }
  const grave = lastRun.status === 'interrupted' || lastRun.status === 'crashed'
  return (
    <div className={grave ? 'error-banner' : 'info-banner'} role="status"
         data-testid="last-run-banner">
      <b>Última corrida registrada — {lastRun.status}</b>
      <div>{lastRun.message}</div>
      {lastRun.resumable && onResume && (
        <button onClick={() => void resume()} disabled={pending || lastRun.resume_requires_cv}>
          {pending ? 'Reanudando…' : 'Reanudar corrida'}
        </button>
      )}
      {error && <div role="alert">{error}</div>}
    </div>
  )
}
