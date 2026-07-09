import { useState } from 'react'
import { postScrape, putConfig } from '../api'
import type { SearchConfigDto } from '../types'

interface Props {
  config: SearchConfigDto
  auditReport: string | null
  ttlHours: number
  isRunning: boolean
  onScrapeStarted: (runId: string) => void
  onReparse: () => void
}

// Fase 2 — el humano valida (y edita) antes de gastar scraping.
// El JSON es editable; Confirmar lo re-valida vía Pydantic (PUT /api/config,
// el gate final) y arranca la corrida (POST /api/scrape).
export default function ValidationPanel({
  config, auditReport, ttlHours, isRunning, onScrapeStarted, onReparse,
}: Props) {
  const [jsonText, setJsonText] = useState(JSON.stringify(config, null, 2))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Patrón "adjusting state when a prop changes" (react.dev) — setState
  // durante el render es legal y no cascadea; un useEffect acá sí.
  const [lastConfig, setLastConfig] = useState(config)
  if (config !== lastConfig) {
    setLastConfig(config)
    setJsonText(JSON.stringify(config, null, 2))
    setError(null)
  }

  const clean =
    auditReport !== null &&
    (auditReport.toLowerCase().includes('no issues detected') ||
      auditReport.toLowerCase().includes('looks accurate'))

  async function handleConfirm() {
    setBusy(true)
    setError(null)
    try {
      let parsed: unknown
      try {
        parsed = JSON.parse(jsonText)
      } catch {
        throw new Error('el JSON no es válido — revisá comas y comillas')
      }
      await putConfig(parsed) // gate Pydantic en el backend
      const { run_id } = await postScrape(ttlHours)
      onScrapeStarted(run_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Validación human-in-the-loop</h3>
        <span className="phase">Fase 2</span>
      </div>
      <p>
        Revisá la configuración extraída. Editala directo si la IA malinterpretó tu
        pedido, y confirmá para arrancar.
      </p>
      {auditReport && (
        <div className={clean ? 'audit-ok' : 'audit-warn'}>
          <span>
            <b>AI Auditor (cross-check):</b> {auditReport}
          </span>
        </div>
      )}
      <textarea
        className="prompt-input"
        style={{ minHeight: 220, fontFamily: 'var(--mono)', fontSize: 12 }}
        value={jsonText}
        onChange={(e) => setJsonText(e.target.value)}
        spellCheck={false}
      />
      <div className="actions">
        <button
          className="btn primary"
          disabled={busy || isRunning}
          onClick={() => void handleConfirm()}
        >
          {busy ? 'Validando…' : 'Confirmar y extraer'}
        </button>
        <button className="btn" disabled={busy || isRunning} onClick={onReparse}>
          Re-parsear
        </button>
      </div>
      {error && (
        <div className="error-banner" style={{ marginTop: 14, marginBottom: 0 }}>
          <code>{error}</code>
        </div>
      )}
    </div>
  )
}
