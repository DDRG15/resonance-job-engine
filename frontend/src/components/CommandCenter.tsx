import { useState } from 'react'
import { postParse } from '../api'
import type { ParseResponse } from '../types'

interface Props {
  onParsed: (result: ParseResponse) => void
}

export default function CommandCenter({ onParsed }: Props) {
  const [prompt, setPrompt] = useState('')
  const [vaultLabel, setVaultLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleParse() {
    setBusy(true)
    setError(null)
    try {
      const result = await postParse(prompt, vaultLabel.trim() || undefined)
      onParsed(result)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Command Center</h3>
        <span className="phase">Fase 1</span>
      </div>
      <p>Describí tu trabajo ideal en lenguaje natural. GEMA se encarga del resto.</p>
      <textarea
        className="prompt-input"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder='Ejemplo: "Necesito roles Senior Python Backend o SDET, 100% remoto, mínimo $90k/año. Excluir posiciones contract e híbridas."'
      />
      <div className="actions">
        <button
          className="btn primary"
          disabled={busy || !prompt.trim()}
          onClick={handleParse}
        >
          {busy ? 'Contactando LLM…' : 'Parse with AI'}
        </button>
        <input
          className="prompt-input"
          style={{ minHeight: 0, height: 38, flex: 1 }}
          value={vaultLabel}
          onChange={(e) => setVaultLabel(e.target.value)}
          placeholder="Guardar en Vault (opcional)"
        />
      </div>
      {error && (
        <div className="error-banner" style={{ marginTop: 14, marginBottom: 0 }}>
          El parse falló: <code>{error}</code>
        </div>
      )}
    </div>
  )
}
