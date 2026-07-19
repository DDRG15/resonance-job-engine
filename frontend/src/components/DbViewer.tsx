import { useEffect, useMemo, useRef, useState } from 'react'
import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import type { ColumnDef, SortingState } from '@tanstack/react-table'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { RegistryJob } from '../types'

interface Props {
  onClose: () => void
}

const TIER_CLASS: Record<string, string> = {
  'Tier 1': 'b1', 'Tier 2': 'b2', 'Tier 3': 'b3', 'Tier 4': 'b4',
}

const COLUMNS: ColumnDef<RegistryJob>[] = [
  {
    accessorKey: 'scraped_at',
    header: 'Capturado',
    cell: (c) => (
      <span style={{ whiteSpace: 'nowrap' }}>
        {(c.getValue<string>() ?? '').slice(0, 16).replace('T', ' ')}
      </span>
    ),
  },
  {
    accessorKey: 'title',
    header: 'Puesto',
    cell: (c) => (
      <a href={c.row.original.url} target="_blank" rel="noopener noreferrer">
        {c.getValue<string>() || '—'}
      </a>
    ),
  },
  { accessorKey: 'company', header: 'Empresa' },
  {
    accessorKey: 'tier',
    header: 'Tier',
    cell: (c) => {
      const tier = c.getValue<string | null>()
      return tier ? (
        <span className={`badge ${TIER_CLASS[tier] ?? ''}`}>{tier}</span>
      ) : (
        <span className="side-note">sin tier</span>
      )
    },
  },
  {
    accessorKey: 'match_score',
    header: 'Score',
    cell: (c) => c.getValue<number | null>() ?? '—',
  },
  {
    accessorKey: 'salary_raw',
    header: 'Salario',
    cell: (c) => c.getValue<string | null>() || '—',
  },
  {
    accessorKey: 'location_raw',
    header: 'Ubicación',
    cell: (c) => c.getValue<string | null>() || '—',
  },
  { accessorKey: 'source_domain', header: 'Fuente' },
]

// F12 + F15a: acceso crudo a TODO lo recopilado (incluidas corridas del cron)
// con TanStack Table (sorting por columna + búsqueda global) y virtualización
// (@tanstack/react-virtual) — 1000 filas se sienten como 20.
export default function DbViewer({ onClose }: Props) {
  const [jobs, setJobs] = useState<RegistryJob[] | null>(null)
  const [last24h, setLast24h] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [sorting, setSorting] = useState<SortingState>([
    { id: 'scraped_at', desc: true },
  ])
  const [globalFilter, setGlobalFilter] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    setJobs(null)
    setError(null)
    const params = last24h ? '?limit=1000&hours=24' : '?limit=1000'
    fetch(`/api/registry/jobs${params}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`)
        return r.json() as Promise<{ jobs: RegistryJob[] }>
      })
      .then((body) => {
        if (!cancelled) setJobs(body.jobs)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [last24h])

  const data = useMemo(() => jobs ?? [], [jobs])
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
    estimateSize: () => 44,
    overscan: 8,
  })
  const virtualRows = virtualizer.getVirtualItems()
  const padTop = virtualRows.length > 0 ? virtualRows[0].start : 0
  const padBottom =
    virtualRows.length > 0
      ? virtualizer.getTotalSize() - virtualRows[virtualRows.length - 1].end
      : 0

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Base de datos — seen registry</h3>
        <span className="phase">crudo · ordenable · buscable</span>
      </div>
      <p>
        Todo lo que GEMA recopiló, incluidas las corridas automáticas del
        scheduler. Clic en una columna para ordenar.
      </p>
      <div className="actions" style={{ marginTop: 0, marginBottom: 12, flexWrap: 'wrap' }}>
        <button
          className={`btn mini ${last24h ? 'primary' : ''}`}
          onClick={() => setLast24h(true)}
        >
          Últimas 24 h
        </button>
        <button
          className={`btn mini ${!last24h ? 'primary' : ''}`}
          onClick={() => setLast24h(false)}
        >
          Todo (máx. 1000)
        </button>
        <input
          className="db-search"
          placeholder="Buscar en todo (puesto, empresa, ubicación…)"
          value={globalFilter}
          onChange={(e) => setGlobalFilter(e.target.value)}
        />
        <span style={{ flex: 1 }} />
        <button className="btn mini" onClick={onClose}>
          Cerrar
        </button>
      </div>

      {error && (
        <div className="error-banner">
          No se pudo leer la base de datos: <code>{error}</code>
        </div>
      )}

      {jobs === null && !error && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '8px 0' }}>
          {[0, 1, 2, 3, 4].map((i) => (
            <div className="skeleton" style={{ width: `${90 - i * 7}%` }} key={i} />
          ))}
        </div>
      )}

      {jobs !== null && rows.length === 0 && (
        <div className="empty-state">
          <span className="glyph">◇</span>
          <b>{globalFilter ? 'Sin coincidencias' : 'Registry vacío en este rango'}</b>
          {globalFilter
            ? 'Probá con otro término de búsqueda.'
            : 'Corré una extracción (o esperá al cron) y esto se llena solo.'}
        </div>
      )}

      {jobs !== null && rows.length > 0 && (
        <>
          <span className="side-note">
            {rows.length} de {jobs.length} filas
          </span>
          <div className="db-scroll" ref={scrollRef}>
            <table className="db-table">
              <thead>
                {table.getHeaderGroups().map((hg) => (
                  <tr key={hg.id}>
                    {hg.headers.map((h) => (
                      <th
                        key={h.id}
                        className="sortable"
                        onClick={h.column.getToggleSortingHandler()}
                      >
                        {flexRender(h.column.columnDef.header, h.getContext())}
                        {{ asc: ' ▲', desc: ' ▼' }[
                          h.column.getIsSorted() as string
                        ] ?? ''}
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {padTop > 0 && (
                  <tr>
                    <td style={{ height: padTop, padding: 0 }} colSpan={COLUMNS.length} />
                  </tr>
                )}
                {virtualRows.map((vr) => {
                  const row = rows[vr.index]
                  return (
                    <tr key={row.id} data-index={vr.index}>
                      {row.getVisibleCells().map((cell) => (
                        <td
                          key={cell.id}
                          className={
                            ['scraped_at', 'match_score', 'source_domain'].includes(
                              cell.column.id,
                            )
                              ? 'mono'
                              : undefined
                          }
                        >
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                  )
                })}
                {padBottom > 0 && (
                  <tr>
                    <td style={{ height: padBottom, padding: 0 }} colSpan={COLUMNS.length} />
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
