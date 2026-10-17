// Aceptación del aviso de envío del CV a proveedores LLM externos. Vive en localStorage y NO en el servidor ni junto a `retain_cv`: aceptar que
// el texto del CV salga a un proveedor y decidir que el perfil derivado quede en disco son
// decisiones distintas, con riesgos distintos, y una no implica la otra.

// Misma versión que config.CV_CONSENT_VERSION (api.py la exige en cada upload y responde
// 428 si no coincide). Se cambia cuando cambia lo que el aviso promete: la aceptación de
// una versión vieja no vale. tests/test_cv_ciclo_y_privacidad.py compara ambas constantes.
export const CV_CONSENT_VERSION = 'cv-llm-2026-09-30'

const KEY = 'gema.cv_llm_consent'

// localStorage puede lanzar (modo privado, cuota, políticas del navegador): ante la duda
// la respuesta es "no hay aceptación" — fallar cerrado, jamás enviar por un error de lectura.
export function hasCvConsent(): boolean {
  try {
    return localStorage.getItem(KEY) === CV_CONSENT_VERSION
  } catch {
    return false
  }
}

export function grantCvConsent(): boolean {
  try {
    localStorage.setItem(KEY, CV_CONSENT_VERSION)
    return true
  } catch {
    return false
  }
}

export function revokeCvConsent(): void {
  try {
    localStorage.removeItem(KEY)
  } catch {
    // sin storage no hay nada que revocar
  }
}
