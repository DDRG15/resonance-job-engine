import type { TieredJobDto } from '../types'

/**
 * Calidad de los campos de un aviso — presentación, no scoring.
 *
 * Por qué existe: medido sobre las 1399 filas del registry el 2026-09-03,
 * `company` reporta 100% de cobertura y es inutilizable en 247 filas (17,7%).
 * La columna es NOT NULL y siempre trae texto, así que ni la DB ni la UI lo
 * notaban: entraba a la tabla, a Notion y al Excel como si fuera un nombre.
 *
 * Los tres modos, con su conteo real:
 *   placeholder del scraper  ·  51 filas  ·  arc.dev, builtin.com
 *   título pegado con \n     ·  60 filas  ·  python.org, el 100% del board
 *   eslogan de marketing     · 136 filas  ·  jobspresso.co
 */

const PLACEHOLDER = /^\(empresa no listada\)$/i

/** Un nombre de empresa de más de 60 caracteres es una frase, no un nombre. */
const LARGO_MAX = 60

export function companyIsDirty(company: string): boolean {
  const c = (company ?? '').trim()
  if (c === '') return true
  if (PLACEHOLDER.test(c)) return true
  if (c.includes('\n')) return true
  return c.length > LARGO_MAX
}

/**
 * Cuántos de los cinco campos que GEMA *puede* saber están realmente presentes.
 * No incluye tier ni score: esos los calcula GEMA, no los extrae del board.
 */
export function fieldConfidence(tj: TieredJobDto): {
  present: number
  total: number
  notches: { key: string; label: string; state: 'on' | 'off' | 'bad' }[]
} {
  const j = tj.job
  const dirty = companyIsDirty(j.company)
  const notches: { key: string; label: string; state: 'on' | 'off' | 'bad' }[] = [
    { key: 'title', label: 'Título', state: j.title?.trim() ? 'on' : 'off' },
    { key: 'company', label: 'Empresa', state: dirty ? 'bad' : 'on' },
    { key: 'url', label: 'URL', state: j.url ? 'on' : 'off' },
    { key: 'location', label: 'Ubicación', state: j.location_raw?.trim() ? 'on' : 'off' },
    { key: 'salary', label: 'Salario', state: j.salary_raw?.trim() ? 'on' : 'off' },
  ]
  return {
    present: notches.filter((n) => n.state === 'on').length,
    total: notches.length,
    notches,
  }
}
