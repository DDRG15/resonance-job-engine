import { useState } from 'react'
import { putGodmode } from '../api'

// God Mode — overrides de selectores cuando un board cambió su HTML.
// Se aplican en el PRÓXIMO parse (misma semántica que Streamlit).
export default function GodMode() {
  const [xpath, setXpath] = useState('')
  const [css, setCss] = useState('')
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleApply() {
    setError(null)
    try {
      await putGodmode(xpath.trim(), css.trim())
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <details className="god">
      <summary>God Mode — selectores avanzados</summary>
      <div className="god-body">
        <span className="side-note">
          Override manual cuando un board cambia su HTML. Se aplica en el próximo parse.
        </span>
        <input
          className="text-input"
          placeholder='XPath del título: //h2[@class="role-header"]'
          value={xpath}
          onChange={(e) => {
            setXpath(e.target.value)
            setSaved(false)
          }}
        />
        <input
          className="text-input"
          placeholder="CSS del job card: div.job-listing-card"
          value={css}
          onChange={(e) => {
            setCss(e.target.value)
            setSaved(false)
          }}
        />
        <button type="button" className="btn mini" onClick={() => void handleApply()}>
          Aplicar overrides
        </button>
        {saved && (
          <span className="side-note" style={{ color: 'var(--t2-ink)' }}>
            God Mode activo — se aplica en el próximo parse.
          </span>
        )}
        {error && <span className="side-note" style={{ color: 'var(--t3-ink)' }}>{error}</span>}
      </div>
    </details>
  )
}
