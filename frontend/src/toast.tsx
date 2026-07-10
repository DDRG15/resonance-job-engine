import { createContext, useCallback, useContext, useRef, useState } from 'react'
import type { ReactNode } from 'react'

// F17 (triaje Kimi r3): toasts — feedback no-bloqueante para acciones que
// hoy pasaban en silencio (cron aplicado, perfil descartado, reset).
// Errores de flujo siguen inline donde ocurren; el toast es para confirmar.

export type ToastKind = 'success' | 'info' | 'warn' | 'error'

interface Toast {
  id: number
  kind: ToastKind
  message: string
}

const ToastContext = createContext<(message: string, kind?: ToastKind) => void>(
  () => {},
)

// eslint-disable-next-line react-refresh/only-export-components
export function useToast() {
  return useContext(ToastContext)
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const push = useCallback((message: string, kind: ToastKind = 'info') => {
    const id = nextId.current++
    setToasts((prev) => [...prev.slice(-4), { id, kind, message }])
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id))
    }, 5000)
  }, [])

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div className={`toast toast-${t.kind}`} key={t.id}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
