import { useRef, useState } from 'react'
import { deleteCv, postCv } from '../api'
import { useToast } from '../toast'
import type { CvProfile } from '../types'

interface Props {
  profile: CvProfile | null
  onChanged: () => void
}

// Tu Perfil — CV efímero: se sube, se convierte en perfil (rol/skills) y el
// crudo se descarta. Nada se persiste salvo que el cron lo pida (retain_cv).
export default function CVUpload({ profile, onChanged }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [fileName, setFileName] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const toast = useToast()

  async function handleFile(file: File) {
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

  async function handleClear() {
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
        onClick={() => inputRef.current?.click()}
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
      {error && <span className="side-note" style={{ color: 'var(--t3-ink)' }}>{error}</span>}
    </div>
  )
}
