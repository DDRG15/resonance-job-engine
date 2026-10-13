import type { Status } from '../types'

function greeting(): string {
  const h = new Date().getHours()
  if (h >= 6 && h < 12) return 'Buenos días'
  if (h >= 12 && h < 19) return 'Buenas tardes'
  return 'Buenas noches'
}

export default function Header({ status }: { status: Status | null }) {
  return (
    <div className="topbar">
      <div className="greet">
        <h2>
          {greeting()}
        </h2>
        <p>
          Motor determinístico a 0 tokens: APIs públicas, ATS (Greenhouse, Lever,
          Ashby) y boards por navegador. El LLM solo interpreta tu búsqueda y
          actúa de fallback.
        </p>
      </div>
      {status && (
        <div className="live-card">
          <div className="lbl">
            Estado en vivo{' '}
            {status.nominal ? (
              <span className="pill-ok">
                <span className="dot" /> Todo en orden
              </span>
            ) : (
              <span className="pill-warn">
                <span className="dot" /> Atención
              </span>
            )}
          </div>
          <div className="big">
            {status.boards_active} boards activos ({status.boards} reg.) ·{' '}
            {status.providers_configured} LLM en cadena
          </div>
          <div className="sub">
            {status.stale_selectors.length > 0
              ? `${status.stale_selectors.length} selectores stale (activos)`
              : 'selectores al día'}{' '}
            ·{' '}
            {status.scheduler.enabled
              ? `próxima corrida: ${status.scheduler.next_run}`
              : 'scheduler en pausa'}
          </div>
        </div>
      )}
    </div>
  )
}
