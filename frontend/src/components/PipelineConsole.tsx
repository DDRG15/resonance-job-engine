import { useEffect, useRef, useState } from 'react'
import type { Progreso } from '../types'
import ProgresoCorrida from './ProgresoCorrida'

interface Props {
  runId: string
  onDone: () => void
}

// Fase 3 — consola en vivo vía SSE (/api/scrape/stream).
// Montar con key={runId}: cada corrida arranca una conexión limpia; el
// backend manda el historial primero, así que reconexiones tardías no
// pierden logs. Al evento `done` cerramos el EventSource (si no, el browser
// reconectaría en loop contra un stream que ya terminó).
export default function PipelineConsole({ runId, onDone }: Props) {
  const [lines, setLines] = useState<string[]>([])
  const [live, setLive] = useState(true)
  // Pieza 4: último bloque `progreso` que mandó el backend (evento SSE `progreso`).
  const [progreso, setProgreso] = useState<Progreso | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const es = new EventSource('/api/scrape/stream')
    es.onmessage = (e) => {
      const data = JSON.parse(e.data) as { log: string }
      setLines((prev) => [...prev, data.log])
    }
    es.addEventListener('progreso', (e) => {
      try {
        setProgreso(JSON.parse((e as MessageEvent).data) as Progreso)
      } catch {
        // un bloque ilegible no puede cortar el stream de logs
      }
    })
    es.addEventListener('done', () => {
      es.close()
      setLive(false)
      // El backend deja de informar apenas termina la corrida: se cierra la barra acá
      // para que el último cuadro no quede clavado en 99% «puntuando».
      setProgreso((p) => (p ? { ...p, fase: 'listo', porcentaje: 100, restante_s: 0,
        excedido: false, ultima_actividad_hace_s: 0, completadas: p.total } : p))
      onDone()
    })
    es.onerror = () => {
      es.close()
      setLive(false)
      // Sin stream no hay datos frescos: una barra congelada se leería como «sigue viva».
      setProgreso(null)
    }
    return () => es.close()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'nearest' })
  }, [lines])

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Consola del pipeline</h3>
        <span className="phase">{live ? 'Fase 3 · corriendo' : 'Fase 3'}</span>
      </div>
      <ProgresoCorrida progreso={progreso} />
      <pre className="console">
        {lines.join('\n')}
        {live && (
          <>
            {'\n'}
            <span className="cursor" />
          </>
        )}
        <div ref={endRef} />
      </pre>
    </div>
  )
}
