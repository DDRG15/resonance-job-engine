import { useRef, useState } from 'react'
import { deleteCv, postCv } from '../api'
import { grantCvConsent, hasCvConsent, revokeCvConsent } from '../consent'
import { useToast } from '../toast'
import type { CvProfile } from '../types'

interface Props {
  profile: CvProfile | null
  // true si el scheduler retiene el perfil en disco (SchedulerState.retain_cv).
  retained?: boolean
  onChanged: () => void
}

// Tu Perfil — CV efímero: se sube, se convierte en perfil (rol/skills) y el
// crudo se descarta. Nada se persiste salvo que el cron lo pida (retain_cv).
//
// El texto del CV sale a un proveedor LLM externo. Antes de
// abrir el selector de archivos la UI dice qué sale y adónde, y pide aceptación explícita;
// sin ella no se abre el selector ni se envía nada (y el servidor responde 428 igual).
// La aceptación se guarda aparte de la retención: son decisiones distintas.
export default function CVUpload({ profile, retained = false, onChanged }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [fileName, setFileName] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [accepted, setAccepted] = useState(hasCvConsent)
  const [showAviso, setShowAviso] = useState(false)
  const toast = useToast()

  async function handleFile(file: File) {
    // Segunda barrera: aunque algo dispare el input sin pasar por el botón, sin
    // aceptación vigente el archivo no sale de aquí.
    if (!accepted) {
      setShowAviso(true)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await postCv(file)
      setFileName(file.name)
      onChanged()
      // F17 restante (pulido 07-17): confirmar la carga que pasaba en silencio
      toast(`CV procesado — perfil activo (${file.name})`, 'success')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function handleSubir() {
    if (!accepted) {
      setShowAviso(true)
      return
    }
    inputRef.current?.click()
  }

  function handleAceptar() {
    grantCvConsent()
    setAccepted(true)
    setShowAviso(false)
    inputRef.current?.click()
  }

  function handleRevocar() {
    revokeCvConsent()
    setAccepted(false)
  }

  async function handleClear() {
    // DELETE /api/cv limpia la API, el scheduler y el archivo retenido (clear_profile)
    await deleteCv()
    setFileName(null)
    onChanged()
    toast('Perfil descartado — los scores vuelven a ser genéricos', 'info')
  }

  return (
    <div className="side-sec">
      <span className="side-label">Tu Perfil</span>
      <input
        ref={inputRef}
        type="file"
        accept=".pdf,.docx,.txt,.md"
        style={{ display: 'none' }}
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) void handleFile(f)
          e.target.value = ''
        }}
      />
      <button
        type="button"
        className="file-chip"
        disabled={busy}
        onClick={handleSubir}
      >
        📄{' '}
        {busy ? (
          'Parseando CV con el LLM…'
        ) : profile ? (
          <b>{fileName ?? 'CV cargado'} — perfil activo</b>
        ) : (
          <span>Subir CV (PDF / DOCX / TXT / MD)</span>
        )}
      </button>
      {showAviso && (
        <div
          role="group"
          aria-label="Aviso de envío del CV"
          className="side-card"
          style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
        >
          <b>Antes de subir tu CV</b>
          <span className="side-note">
            Se envía el <b>texto completo</b> del archivo: tu nombre, tus datos de contacto y
            todo tu historial laboral y educativo. Tu email y tu teléfono se tachan en tu
            máquina antes de salir; el nombre y el historial salen tal cual.
          </span>
          <span className="side-note">
            Va a un <b>proveedor LLM externo</b> (Groq, Gemini, OpenRouter, Cohere, Cerebras,
            SambaNova o Mistral): el primero de tu cadena que responda y, si falla, el
            siguiente recibe el mismo texto. A tu máquina no vuelve el CV, solo un perfil
            (rol, skills, región).
          </span>
          <div style={{ display: 'flex', gap: 6 }}>
            <button type="button" className="btn mini primary" onClick={handleAceptar}>
              Acepto y elegir archivo
            </button>
            <button type="button" className="btn mini" onClick={() => setShowAviso(false)}>
              Cancelar
            </button>
          </div>
        </div>
      )}
      {profile && (
        <>
          <div className="side-card">
            <div className="row">
              <span>Rol</span>
              <span>{profile.role ?? '?'}</span>
            </div>
            <div className="row">
              <span>Skills</span>
              <span>{profile.core_skills?.length ?? 0}</span>
            </div>
            <div className="row">
              <span>Región</span>
              <span>{profile.location ?? '?'}</span>
            </div>
          </div>
          {retained && (
            <span className="side-note">
              Perfil retenido en disco (solo rol, skills y región — nunca el CV) para que el
              cron lo use entre corridas. «Descartar perfil» lo borra de memoria y de disco.
            </span>
          )}
          <button type="button" className="btn mini" onClick={() => void handleClear()}>
            Descartar perfil
          </button>
        </>
      )}
      {!profile && !busy && (
        <span className="side-note">
          Sin CV los scores de match son genéricos. Nada se guarda más allá de la sesión.
        </span>
      )}
      {accepted && !showAviso && (
        <button
          type="button"
          className="btn mini"
          title="Vuelve a mostrar el aviso antes del próximo envío"
          onClick={handleRevocar}
        >
          Revocar aviso
        </button>
      )}
      {error && <span className="side-note" style={{ color: 'var(--t3-ink)' }}>{error}</span>}
    </div>
  )
}
