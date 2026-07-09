import { useState } from 'react'

interface Props {
  onDone: () => void
}

// Primera corrida (beta testers): sin LLM keys la app no puede parsear.
// POST /api/setup escribe las keys al .env del backend (mismo flujo que
// el wizard de Streamlit).
export default function SetupWizard({ onDone }: Props) {
  const [groq, setGroq] = useState('')
  const [gemini, setGemini] = useState('')
  const [discord, setDiscord] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSave() {
    if (!groq.trim() && !gemini.trim()) {
      setError('Necesitás al menos una LLM key. Groq es gratis en console.groq.com')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await fetch('/api/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          groq_key: groq.trim(),
          gemini_key: gemini.trim(),
          discord_url: discord.trim(),
          validate_keys: true,
        }),
      })
      if (!res.ok) throw new Error(await res.text())
      onDone()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="wizard-wrap">
      <h2>
        Welcome to <em>GEMA</em>
      </h2>
      <p>Configuración inicial — toma unos 2 minutos.</p>

      <div className="wizard-field">
        <label>
          Groq API Key (recomendada — gratis en{' '}
          <a href="https://console.groq.com" target="_blank" rel="noopener noreferrer"
             style={{ color: 'var(--accent)' }}>
            console.groq.com
          </a>
          )
        </label>
        <input
          className="text-input" type="password" placeholder="gsk_..."
          value={groq} onChange={(e) => setGroq(e.target.value)}
        />
      </div>

      <div className="wizard-field">
        <label>Gemini API Key (respaldo opcional)</label>
        <input
          className="text-input" type="password" placeholder="AIza..."
          value={gemini} onChange={(e) => setGemini(e.target.value)}
        />
      </div>

      <div className="wizard-field">
        <label>Discord Webhook URL (opcional — alertas de jobs Tier 1)</label>
        <input
          className="text-input" placeholder="https://discord.com/api/webhooks/..."
          value={discord} onChange={(e) => setDiscord(e.target.value)}
        />
      </div>

      {error && <div className="error-banner">{error}</div>}

      <button
        className="btn primary" disabled={busy} onClick={() => void handleSave()}
        style={{ width: '100%', marginTop: 8 }}
      >
        {busy ? 'Verificando y guardando…' : 'Guardar y lanzar GEMA →'}
      </button>
    </div>
  )
}
