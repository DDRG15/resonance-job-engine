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
          {greeting()}. <em>¿Qué buscamos hoy?</em>
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
          {/* Antes: "17 boards activos (17 reg.)" — dos conteos distintos que coincidían
              por casualidad y se leían como "todo lo registrado funciona". "Activa" es
              "se intenta en cada corrida", no "devuelve avisos": eso lo dice Fuentes. */}
          <div className="big">
            {status.boards_active} fuentes activas · {status.providers_configured}{' '}
            {status.providers_configured === 1 ? 'proveedor LLM con key' : 'proveedores LLM con key'}
          </div>
          {status.boards_por_via && (
            <div className="sub" data-testid="boards-por-via">
              {status.boards_por_via.navegador} por navegador · {status.boards_por_via.api} por
              API · {status.boards_por_via.ats} ATS
              {status.ats_empresas !== undefined && ` (${status.ats_empresas} empresas)`}
              {!!status.boards_retired && ` · ${status.boards_retired} retiradas`}
            </div>
          )}
          <div className="sub">
            {status.stale_selectors.length > 0
              ? `${status.stale_selectors.length} selectores sin verificar hace +30 días`
              : 'selectores verificados en los últimos 30 días'}{' '}
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
