import { useMemo, useRef, useState } from 'react'
import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import type { ColumnDef, SortingState } from '@tanstack/react-table'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { ResultsResponse, TieredJobDto } from '../types'
import JobDetail from './JobDetail'
import { companyIsDirty } from './jobQuality'

const TIERS = ['Tier 1', 'Tier 2', 'Tier 3'] as const
const TIER_CLASS: Record<string, string> = {
  'Tier 1': 'b1', 'Tier 2': 'b2', 'Tier 3': 'b3',
}
const TIER_STRIPE: Record<string, string> = {
  'Tier 1': 's1', 'Tier 2': 's2', 'Tier 3': 's3',
}

const COLUMNS: ColumnDef<TieredJobDto>[] = [
  {
    id: 'tier',
    accessorFn: (r) => r.tier,
    header: 'Tier',
    cell: (c) => (
      <span className={`badge mini-badge ${TIER_CLASS[c.getValue<string>()] ?? ''}`}>
        {c.getValue<string>().replace('Tier ', 'T')}
      </span>
    ),
  },
  {
    id: 'title',
    accessorFn: (r) => r.job.title,
    header: 'Puesto',
    cell: (c) => <span className="cell-title">{c.getValue<string>() || '—'}</span>,
  },
  {
    id: 'company',
    accessorFn: (r) => r.job.company,
    header: 'Empresa',
    cell: (c) => {
      const v = c.getValue<string>()
      // 247 de 1399 filas traen algo que no es un nombre de empresa. Mostrarlas
      // como si lo fueran es peor que no mostrarlas: el dato entra a Notion igual.
      return companyIsDirty(v, c.row.original.job.empresa_no_confirmada)
        ? <span className="cell-dirty" title={v.replace(/\n/g, ' · ')}>sin empresa</span>
        : <span className="cell-dim">{v}</span>
    },
  },
  {
    id: 'location',
    accessorFn: (r) => r.job.location_raw ?? '',
    header: 'Ubicación',
    cell: (c) => {
      const v = c.getValue<string>()
      const tj = c.row.original
      // Tres estados, no dos. Medido sobre los 274 Tier 1 del 17-09: 143 FLAG con
      // ubicación (USA, Canada, Bangalore), 94 PASS SIN ubicación y 37 PASS con
      // ubicación. Dibujar los 94 igual que los 37 convierte "nadie dijo nada" en
      // "está abierto", y es lo que hace que veinte pestañas terminen en un
      // formulario que pide autorización para trabajar en Estados Unidos.
      //
      // La jerarquía es de peso, no de color: los tiers ya se quedaron con la
      // paleta y está validada para daltonismo. El glifo carga el significado.
      if (tj.location_verdict === 'FLAG') {
        return (
          <span
            className="cell-flag"
            title={tj.location_note || 'Región restringida, sin requisito declarado en el aviso'}
          >
            ⚑ {v || 'región restringida'}
          </span>
        )
      }
      // El guión mudo se lee como bug de la UI. El punto medio dice "se buscó".
      return v
        ? <span className="cell-dim" title={tj.location_note || undefined}>{v}</span>
        : <span className="cell-absent" title="El board no la expone: pasa por ausencia de dato, no por evidencia">·</span>
    },
  },
  {
    id: 'match_score',
    accessorFn: (r) => r.match_score,
    header: 'Score',
    cell: (c) => {
      const v = c.getValue<number>()
      return (
        <>
          <span className="cell-score">{v}</span>
          {c.row.original.salario_no_confirmado && (
            <span
              className="cell-nosal"
              title="Salario publicado en texto que no se pudo leer: puntuado sin el eje de salario"
            >
              $?
            </span>
          )}
        </>
      )
    },
  },
]

/**
 * Resultados del run — tabla virtualizada a la izquierda, detalle a la derecha.
 *
 * Reemplaza la lista de cards por tier. Tres razones medidas sobre las
 * 1399 filas del registry el 2026-09-03, no preferencia:
 *
 *  1. La columna de salario mostraba «—» en 1392 de 1399 filas. Una columna de
 *     ancho fijo que casi nunca tiene contenido es ruido: el salario pasa al
 *     detalle, donde aparece solo si existe.
 *  2. La ubicación no se mostraba en ninguna parte, y con 15 de 17 boards sin
 *     selector de ubicación es el campo que decide si un Tier 1 sirve desde el país del candidato.
 *  3. Las cards apilaban las cuatro secciones de tier en vertical, así que ver el
 *     puesto 40 exigía scrollear cuatro encabezados. La tabla virtualizada muestra
 *     ~25 de una y ordena por cualquier columna.
 *
 * El drawer modal se fue por el mismo motivo: tapaba la lista, y comparar dos
 * avisos obligaba a abrir, leer, cerrar y abrir de nuevo.
 */
export default function Results({ results }: { results: ResultsResponse }) {
  const [sorting, setSorting] = useState<SortingState>([
    { id: 'match_score', desc: true },
  ])
  const [globalFilter, setGlobalFilter] = useState('')
  const [tierOff, setTierOff] = useState<Set<string>>(new Set())
  const [board, setBoard] = useState('')
  const [selUrl, setSelUrl] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const todos = useMemo(() => {
    if (!results.tiers) return []
    const t = results.tiers
    return [...t.tier1, ...t.tier2, ...t.tier3]
  }, [results.tiers])

  const conteoTier = useMemo(() => {
    const m: Record<string, number> = {}
    for (const tj of todos) m[tj.tier] = (m[tj.tier] ?? 0) + 1
    return m
  }, [todos])

  const boards = useMemo(
    () => [...new Set(todos.map((t) => t.job.source_domain))].sort(),
    [todos],
  )

  const data = useMemo(
    () => todos.filter(
      (t) => !tierOff.has(t.tier) && (board === '' || t.job.source_domain === board),
    ),
    [todos, tierOff, board],
  )

  const table = useReactTable({
    data,
    columns: COLUMNS,
    state: { sorting, globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  })

  const rows = table.getRowModel().rows
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 42,
    overscan: 10,
  })

  // Si el contenedor mide 0 de alto, el virtualizador no devuelve NINGUNA fila y
  // la tabla se ve vacía teniendo datos cargados — el peor modo de falla posible
  // para esta vista. Ocurre en jsdom (no hay layout) y ocurriría en el navegador
  // si theme.css no cargara. Antes que una tabla en blanco, se rinde un tramo
  // acotado sin virtualizar: se ve la data, y el tope evita clavar el navegador
  // con las 1399 filas.
  const vRows = virtualizer.getVirtualItems()
  const sinMedida = rows.length > 0 && vRows.length === 0
  const idxVisibles = sinMedida
    ? rows.slice(0, 100).map((_, i) => i)
    : vRows.map((v) => v.index)
  const padTop = sinMedida || vRows.length === 0 ? 0 : vRows[0].start
  const padBottom =
    sinMedida || vRows.length === 0
      ? 0
      : virtualizer.getTotalSize() - vRows[vRows.length - 1].end

  // Selección derivada, sin useEffect: si la elegida ya no pasa los filtros, cae
  // sobre la primera visible. El panel nunca queda en blanco teniendo filas.
  const idxSel = Math.max(0, rows.findIndex((r) => r.original.job.url === selUrl))
  const sel = rows[idxSel]?.original ?? null

  function mover(delta: number) {
    if (rows.length === 0) return
    const next = Math.min(rows.length - 1, Math.max(0, idxSel + delta))
    setSelUrl(rows[next].original.job.url)
    virtualizer.scrollToIndex(next, { align: 'auto' })
  }

  if (!results.tiers) return null
  const total = todos.length

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Resultados</h3>
        <span className="phase">{total} avisos · {boards.length} boards</span>
      </div>

      {results.summary && results.summary.errors.length > 0 && (
        <div className="error-banner">
          {results.summary.errors.length} error(es) durante el run — los resultados
          pueden estar incompletos:
          {results.summary.errors.map((e, i) => (
            <div key={i}><code>{e}</code></div>
          ))}
        </div>
      )}

      <div className="res-toolbar">
        <div className="tier-filters" role="group" aria-label="Filtrar por tier">
          {TIERS.map((t) => {
            const n = conteoTier[t] ?? 0
            const off = tierOff.has(t)
            return (
              <button
                key={t}
                type="button"
                className={`tier-chip ${TIER_CLASS[t]} ${off ? 'off' : ''}`}
                aria-pressed={!off}
                disabled={n === 0}
                onClick={() => {
                  const s = new Set(tierOff)
                  if (off) s.delete(t)
                  else s.add(t)
                  setTierOff(s)
                }}
              >
                {t.replace('Tier ', 'T')} <b>{n}</b>
              </button>
            )
          })}
        </div>

        <select
          className="res-select"
          value={board}
          onChange={(e) => setBoard(e.target.value)}
          aria-label="Filtrar por board"
        >
          <option value="">Todos los boards</option>
          {boards.map((b) => <option key={b} value={b}>{b}</option>)}
        </select>

        <input
          className="res-search"
          type="search"
          placeholder="Buscar puesto o empresa…"
          value={globalFilter}
          onChange={(e) => setGlobalFilter(e.target.value)}
          aria-label="Buscar"
        />

        {total > 0 && (
          <a className="btn mini" href="/api/export.xlsx" download>
            Exportar {total}
          </a>
        )}
      </div>

      <div className="split">
        <div
          className="split-list"
          ref={scrollRef}
          tabIndex={0}
          role="listbox"
          aria-label="Avisos encontrados"
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); mover(1) }
            if (e.key === 'ArrowUp') { e.preventDefault(); mover(-1) }
          }}
        >
          <table className="res-table">
            <thead>
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id}>
                  {hg.headers.map((h) => (
                    <th
                      key={h.id}
                      className={`col-${h.column.id}`}
                      onClick={h.column.getToggleSortingHandler()}
                    >
                      {flexRender(h.column.columnDef.header, h.getContext())}
                      <span className="sort-caret">
                        {{ asc: '▲', desc: '▼' }[h.column.getIsSorted() as string] ?? ''}
                      </span>
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody>
              {padTop > 0 && <tr style={{ height: padTop }}><td colSpan={5} /></tr>}
              {idxVisibles.map((i) => {
                const row = rows[i]
                const tj = row.original
                const activa = tj.job.url === sel?.job.url
                return (
                  <tr
                    key={tj.job.url}
                    className={`res-row ${TIER_STRIPE[tj.tier]} ${activa ? 'sel' : ''}`}
                    role="option"
                    aria-selected={activa}
                    onClick={() => setSelUrl(tj.job.url)}
                  >
                    {row.getVisibleCells().map((c) => (
                      <td key={c.id} className={`col-${c.column.id}`}>
                        {flexRender(c.column.columnDef.cell, c.getContext())}
                      </td>
                    ))}
                  </tr>
                )
              })}
              {padBottom > 0 && <tr style={{ height: padBottom }}><td colSpan={5} /></tr>}
              {rows.length === 0 && (
                <tr><td colSpan={5} className="res-none">
                  Ningún aviso pasa los filtros. {total} en total.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="split-detail">
          <JobDetail tj={sel} />
        </div>
      </div>
    </div>
  )
}
