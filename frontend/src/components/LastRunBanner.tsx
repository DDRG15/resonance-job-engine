import type { LastRunDto } from '../types'

/**
 * Qué se sabe del último run cuando la sesión no tiene resultados. Tras reiniciar el servidor la memoria queda vacía; la fila de `runs` en la
 * DB sobrevive y dice cómo terminó la corrida. El mensaje lo arma el backend para que
 * esta pantalla no tenga que adivinar qué quedó disponible y qué no.
 */
export default function LastRunBanner({ lastRun }: { lastRun: LastRunDto }) {
  const grave = lastRun.status === 'interrupted' || lastRun.status === 'crashed'
  return (
    <div className={grave ? 'error-banner' : 'info-banner'} role="status"
         data-testid="last-run-banner">
      <b>Última corrida registrada — {lastRun.status}</b>
      <div>{lastRun.message}</div>
    </div>
  )
}
