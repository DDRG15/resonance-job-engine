import { useEffect, useRef, useState } from 'react'

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
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const es = new EventSource('/api/scrape/stream')
    es.onmessage = (e) => {
      const data = JSON.parse(e.data) as { log: string }
      setLines((prev) => [...prev, data.log])
    }
    es.addEventListener('done', () => {
      es.close()
      setLive(false)
      onDone()
    })
    es.onerror = () => {
      es.close()
      setLive(false)
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
