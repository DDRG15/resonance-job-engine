import type { FaseProgreso, Progreso } from '../types'

// Pieza 4 — barra de progreso de la corrida.
//
// Escenario: una corrida dura 10-20 minutos y, sin una señal de avance, una viva se ve
// igual que una colgada: el usuario cierra la ventana, relanza y choca con el 409.
// Este componente solo MUESTRA lo que manda el backend; no inventa tiempos ni cancela
// nada. El aviso de inactividad es eso, un aviso: no hay acción asociada porque no existe
// cancelación real (ver RunManager).

const FASES: Record<FaseProgreso, string> = {
  endpoints: 'Leyendo APIs…',
  ats: 'Leyendo tableros de empresas…',
  gmail: 'Leyendo alertas de correo…',
  navegador: 'Recorriendo boards con el navegador…',
  puntuando: 'Puntuando y guardando…',
  listo: 'Listo',
}

// Pasado este umbral sin una sola línea de log ni unidad terminada, se avisa. 120 s: una
// rama del navegador lenta tarda 30-60 s; dos minutos de silencio ya no es lentitud normal.
const UMBRAL_INACTIVIDAD_S = 120

/** 192 -> «3:12»; 3725 -> «1:02:05». */
function formatearDuracion(s: number): string {
  const t = Math.max(0, Math.floor(s))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const seg = t % 60
  const ss = String(seg).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

/** Restante aproximado: no se promete precisión de segundos sobre una estimación. */
function formatearRestante(s: number): string {
  if (s < 45) return 'menos de 1 min'
  const min = Math.round(s / 60)
  if (min < 60) return `~${Math.max(1, min)} min`
  const h = Math.floor(min / 60)
  const resto = min % 60
  return resto ? `~${h} h ${resto} min` : `~${h} h`
}

function textoRestante(p: Progreso): string {
  if (p.fase === 'listo') return 'Terminó'
  if (p.excedido) return 'Está tardando más de lo habitual'
  if (p.restante_s === null) return 'Restante: calculando…'
  if (p.restante_s === 0) return 'Terminando…'
  return `Restan ${formatearRestante(p.restante_s)}`
}

function textoActividad(s: number): string {
  if (s < 1) return 'ahora mismo'
  if (s < 60) return `hace ${s} s`
  return `hace ${Math.floor(s / 60)} min ${s % 60 ? `${s % 60} s` : ''}`.trim()
}

interface Props {
  progreso: Progreso | null
}

export default function ProgresoCorrida({ progreso: p }: Props) {
  if (!p) return null
  const listo = p.fase === 'listo'
  const sinActividad = !listo && p.ultima_actividad_hace_s > UMBRAL_INACTIVIDAD_S
  const pct = Math.max(0, Math.min(100, Math.round(p.porcentaje)))
  const etiqueta = FASES[p.fase] ?? p.fase

  return (
    <div className="progreso" data-testid="progreso-corrida">
      <div className="progreso-head">
        <span className="progreso-fase">Fase: {etiqueta}</span>
        <span className="progreso-pct">{pct}%</span>
      </div>
      <div
        className="progreso-bar"
        role="progressbar"
        aria-label="Progreso de la corrida"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-valuetext={`${pct}% · ${etiqueta}`}
      >
        <div className={`progreso-fill${listo ? ' listo' : ''}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="progreso-meta">
        <span>Transcurrido {formatearDuracion(p.transcurrido_s)}</span>
        <span className={p.excedido ? 'progreso-excedido' : undefined}>{textoRestante(p)}</span>
        {!listo && <span>Última actividad {textoActividad(p.ultima_actividad_hace_s)}</span>}
        {p.total > 0 && <span>{p.completadas} de {p.total} unidades</span>}
      </div>
      {!listo && p.estimado_desde === 'corridas' && !p.excedido && p.restante_s !== null && (
        <p className="side-note">
          Estimación gruesa: todavía no hay historial de esta etapa y se calcula con la
          duración de corridas anteriores.
        </p>
      )}
      {sinActividad && (
        <div className="info-banner" role="alert" style={{ marginTop: 10, marginBottom: 0 }}>
          Sin actividad hace {Math.max(1, Math.floor(p.ultima_actividad_hace_s / 60))} min —
          puede estar trabado. Esperá un rato más antes de relanzar: una segunda corrida
          mientras esta sigue viva se rechaza.
        </div>
      )}
    </div>
  )
}
