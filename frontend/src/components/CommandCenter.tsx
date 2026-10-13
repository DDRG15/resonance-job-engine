import { useState } from 'react'
import { postParse } from '../api'
import { useToast } from '../toast'
import type { CvProfile, ParseResponse } from '../types'

interface Props {
  profile: CvProfile | null
  onParsed: (result: ParseResponse) => void
}

// F10 (idea de el usuario, live test 07-10): generar el prompt desde el perfil del
// CV — un clic, y el humano lo revisa/edita antes de parsear. Human-in-the-
// loop intacto: el botón solo PRE-LLENA el textarea, jamás dispara la
// búsqueda solo.
function promptFromProfile(p: CvProfile): string {
  const roles = (p.role ?? '')
    .split(/\s*[|·/]\s*/)
    .map((r) => r.trim())
    .filter(Boolean)
  const skills = (p.core_skills ?? []).slice(0, 10)
  const parts = [
    `Necesito posiciones 100% remotas de: ${roles.join(', ') || 'Software Engineer'}.`,
  ]
  if (skills.length > 0) {
    parts.push(`Tecnologías clave: ${skills.join(', ')}.`)
  }
  parts.push(
    'Cualquier seniority: junior, mid-level o senior.',
    'Salario mínimo $30k/año.',
  )
  return parts.join(' ')
}

export default function CommandCenter({ profile, onParsed }: Props) {
  const [prompt, setPrompt] = useState('')
  const [vaultLabel, setVaultLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const toast = useToast()

  async function handleParse() {
    setBusy(true)
    setError(null)
    try {
      const label = vaultLabel.trim()
      const result = await postParse(prompt, label || undefined)
      onParsed(result)
      // F17 restante (pulido 07-17): confirmar lo que antes pasaba en silencio
      toast(
        label
          ? `Configuración lista y guardada en el Vault: «${label}»`
          : 'Configuración lista — revisá la validación',
        'success',
      )
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
      <p>Describí tu trabajo ideal en lenguaje natural. Resonance se encarga del resto.</p>
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
          {busy ? 'Interpretando…' : 'Interpretar con IA'}
        </button>
        {profile && (
          <button
            className="btn"
            disabled={busy}
            title="Genera el prompt desde tu CV — lo podés editar antes de parsear"
            onClick={() => setPrompt(promptFromProfile(profile))}
          >
            Usar mi perfil
          </button>
        )}
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
