import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getConfig, getCvProfile, getRegistryStats, getResults, getScheduler, getStatus, getVault,
  loadVaultEntry,
} from './api'
import CommandCenter from './components/CommandCenter'
import DbViewer from './components/DbViewer'
import Header from './components/Header'
import LastRunBanner from './components/LastRunBanner'
import Pipeline from './components/Pipeline'
import Duplicados from './components/Duplicados'
import PipelineConsole from './components/PipelineConsole'
import Results from './components/Results'
import ReviewPanel from './components/ReviewPanel'
import SetupWizard from './components/SetupWizard'
import Sidebar from './components/Sidebar'
import StatsBoard from './components/StatsBoard'
import StatTiles from './components/StatTiles'
import ValidationPanel from './components/ValidationPanel'
import type { AuditVerdict, ParseResponse, ResultsResponse, SearchConfigDto } from './types'

export default function App() {
  const qc = useQueryClient()

  // Estado servidor — cacheado e invalidado explícitamente tras cada acción
  const statusQ = useQuery({ queryKey: ['status'], queryFn: getStatus })
  const statsQ = useQuery({ queryKey: ['registry'], queryFn: getRegistryStats })
  const vaultQ = useQuery({ queryKey: ['vault'], queryFn: getVault })
  const schedQ = useQuery({ queryKey: ['scheduler'], queryFn: getScheduler })
  const cvQ = useQuery({ queryKey: ['cv'], queryFn: getCvProfile })
  const reviewQ = useQuery({
    queryKey: ['review'],
    queryFn: () =>
      fetch('/api/review').then((r) => r.json() as Promise<{ stats: { pending: number } }>),
  })
  // El contador del botón: 119 decisiones esperando no se ven si no se cuentan.
  const dupQ = useQuery({
    queryKey: ['correlators'],
    queryFn: () =>
      fetch('/api/correlators').then((r) => r.json() as Promise<{ total: number }>),
  })

  // Fase 9: el badge de Postulaciones. Cuenta las vencidas con el MISMO cálculo del
  // backend (`vencidas` viaja en cada respuesta de /api/applications); recalcular acá
  // con el reloj del navegador daría otro número que el reporte de Discord.
  const appsQ = useQuery({
    queryKey: ['applications-overdue'],
    queryFn: () =>
      fetch('/api/applications?overdue=true').then((r) => r.json() as Promise<{ vencidas: number }>),
  })

  // revision externa 2026-09-30, punto 9: el último run según la DB. Tras reiniciar el servidor la
  // sesión no tiene resultados, y sin esto la pantalla quedaba vacía sin decir por qué.
  const lastRunQ = useQuery({ queryKey: ['last-run'], queryFn: getResults })
  const lastRun = lastRunQ.data?.last_run ?? null

  // Estado de sesión local (espejo del server-side de api.py)
  const [config, setConfig] = useState<SearchConfigDto | null>(null)
  const [auditReport, setAuditReport] = useState<string | null>(null)
  const [auditVerdict, setAuditVerdict] = useState<AuditVerdict | null>(null)
  const [ttlHours, setTtlHours] = useState(0)
  const [runId, setRunId] = useState<string | null>(null)
  const [results, setResults] = useState<ResultsResponse | null>(null)
  const [showDb, setShowDb] = useState(false)
  const [showStats, setShowStats] = useState(false)
  const [showReview, setShowReview] = useState(false)
  const [showPipeline, setShowPipeline] = useState(false)
  const [showDup, setShowDup] = useState(false)

  const status = statusQ.data ?? null
  const backendDown = statusQ.isError

  // TTL: se inicializa del primer status; después el slider es la fuente
  // de verdad (setState durante render — patrón react.dev, sin useEffect).
  const [ttlInitialized, setTtlInitialized] = useState(false)
  if (status && !ttlInitialized) {
    setTtlInitialized(true)
    setTtlHours(status.ttl_hours)
  }

  // Rehidratar los resultados al cargar. Sin esto, refrescar la pestaña borraba
  // la vista de resultados aunque el server siguiera teniéndolos: `results` solo
  // se llenaba al terminar un run en ESTA pestaña, así que /api/results quedaba
  // servido y nadie lo pedía. Mismo patrón que el TTL — setState en render.
  const [hidratado, setHidratado] = useState(false)
  if (status?.has_results && !status.is_running && !hidratado && results === null) {
    setHidratado(true)
    void getResults().then(setResults)
  }

  // Rehidratar la búsqueda confirmada y una corrida EN CURSO (2026-09-29, hallado por
  // la auditoría de revision externa). Refrescar a mitad de una corrida perdía el panel de
  // progreso aunque el SSE reenvía el historial a quien se conecta tarde: solo
  // faltaba volver a pedir el run_id. Una sola vez por carga — un Reset posterior
  // deja config en null a propósito y no tiene que volver a aparecer.
  const [sesionHidratada, setSesionHidratada] = useState(false)
  if (status && !sesionHidratada) {
    setSesionHidratada(true)
    void getConfig().then((r) => {
      if (r.config) {
        setConfig(r.config)
        setAuditReport(r.audit_report)
        setAuditVerdict(r.audit_verdict)
      }
    })
    if (status.is_running) {
      void getResults().then((r) => { if (r.run_id) setRunId(r.run_id) })
    }
  }

  const refresh = () => void qc.invalidateQueries()

  function handleParsed(result: ParseResponse) {
    setConfig(result.config)
    setAuditReport(result.audit_report)
    setAuditVerdict(result.audit_verdict)
    refresh()
  }

  async function handleLoadVault(vaultId: string) {
    const { config: cfg } = await loadVaultEntry(vaultId)
    setConfig(cfg)
    setAuditReport(null)
    setAuditVerdict(null)
    refresh()
  }

  function handleScrapeStarted(id: string) {
    setResults(null)
    setRunId(id)
    // El status trae `scrape_gate_busy`: sin recargarlo, "Limpiar sesión" seguiría
    // habilitado hasta la próxima invalidación y el clic terminaría en 409.
    refresh()
  }

  async function handleRunDone() {
    setResults(await getResults())
    refresh()
  }

  function handleReset() {
    setConfig(null)
    setAuditReport(null)
    setAuditVerdict(null)
    setRunId(null)
    setResults(null)
    refresh()
  }

  if (status?.needs_setup) {
    return <SetupWizard onDone={refresh} />
  }

  return (
    <div className="app">
      <Sidebar
        status={status}
        stats={statsQ.data ?? null}
        vault={vaultQ.data?.entries ?? []}
        scheduler={schedQ.data ?? null}
        profile={cvQ.data?.profile ?? null}
        ttlHours={ttlHours}
        onTtlChange={setTtlHours}
        onLoadVault={(id) => void handleLoadVault(id)}
        onChanged={refresh}
        onKill={handleReset}
        onOpenDb={() => { setShowDb(true); setShowStats(false); setShowReview(false) }}
        onOpenStats={() => { setShowStats(true); setShowDb(false); setShowReview(false) }}
        onOpenReview={() => {
          setShowReview(true); setShowDb(false); setShowStats(false); setShowPipeline(false)
        }}
        onOpenPipeline={() => {
          setShowPipeline(true); setShowDb(false); setShowStats(false); setShowReview(false)
          setShowDup(false)
        }}
        onOpenDuplicados={() => {
          setShowDup(true); setShowPipeline(false); setShowDb(false); setShowStats(false)
          setShowReview(false)
        }}
        reviewPending={reviewQ.data?.stats.pending ?? 0}
        duplicadosPending={dupQ.data?.total ?? 0}
        overdueApps={appsQ.data?.vencidas ?? 0}
      />
      <main>
        {backendDown && (
          <div className="error-banner">
            Backend no disponible. Levantalo con{' '}
            <code>uvicorn api:app --port 8000 --workers 1</code> y recargá.
          </div>
        )}
        <Header status={status} />
        {showStats && <StatsBoard onClose={() => setShowStats(false)} />}
        {showDb && <DbViewer onClose={() => setShowDb(false)} />}
        {showReview && (
          <ReviewPanel onClose={() => setShowReview(false)} onChanged={refresh} />
        )}
        {showPipeline && (
          <Pipeline onClose={() => setShowPipeline(false)}
                    onChanged={() => void qc.invalidateQueries({ queryKey: ['applications-overdue'] })} />
        )}
        {showDup && (
          <Duplicados onClose={() => setShowDup(false)}
                      onChanged={() => void dupQ.refetch()} />
        )}
        {results === null && runId === null && !status?.is_running && lastRun && (
          <LastRunBanner lastRun={lastRun} />
        )}
        {results && <StatTiles results={results} />}
        <CommandCenter profile={cvQ.data?.profile ?? null} onParsed={handleParsed} />
        {config && (
          <ValidationPanel
            config={config}
            auditReport={auditReport}
            auditVerdict={auditVerdict}
            ttlHours={ttlHours}
            isRunning={runId !== null && results === null}
            onScrapeStarted={handleScrapeStarted}
            onReparse={() => {
              setConfig(null)
              setAuditReport(null)
              setAuditVerdict(null)
            }}
          />
        )}
        {runId && (
          <PipelineConsole runId={runId} onDone={() => void handleRunDone()} key={runId} />
        )}
        {results && <Results results={results} />}
      </main>
    </div>
  )
}
