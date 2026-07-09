import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getCvProfile, getRegistryStats, getResults, getScheduler, getStatus, getVault,
  loadVaultEntry,
} from './api'
import CommandCenter from './components/CommandCenter'
import Header from './components/Header'
import PipelineConsole from './components/PipelineConsole'
import Results from './components/Results'
import SetupWizard from './components/SetupWizard'
import Sidebar from './components/Sidebar'
import StatTiles from './components/StatTiles'
import ValidationPanel from './components/ValidationPanel'
import type { ParseResponse, ResultsResponse, SearchConfigDto } from './types'

export default function App() {
  const qc = useQueryClient()

  // Estado servidor — cacheado e invalidado explícitamente tras cada acción
  const statusQ = useQuery({ queryKey: ['status'], queryFn: getStatus })
  const statsQ = useQuery({ queryKey: ['registry'], queryFn: getRegistryStats })
  const vaultQ = useQuery({ queryKey: ['vault'], queryFn: getVault })
  const schedQ = useQuery({ queryKey: ['scheduler'], queryFn: getScheduler })
  const cvQ = useQuery({ queryKey: ['cv'], queryFn: getCvProfile })

  // Estado de sesión local (espejo del server-side de api.py)
  const [config, setConfig] = useState<SearchConfigDto | null>(null)
  const [auditReport, setAuditReport] = useState<string | null>(null)
  const [ttlHours, setTtlHours] = useState(0)
  const [runId, setRunId] = useState<string | null>(null)
  const [results, setResults] = useState<ResultsResponse | null>(null)

  const status = statusQ.data ?? null
  const backendDown = statusQ.isError

  // TTL: se inicializa del primer status; después el slider es la fuente
  // de verdad (setState durante render — patrón react.dev, sin useEffect).
  const [ttlInitialized, setTtlInitialized] = useState(false)
  if (status && !ttlInitialized) {
    setTtlInitialized(true)
    setTtlHours(status.ttl_hours)
  }

  const refresh = () => void qc.invalidateQueries()

  function handleParsed(result: ParseResponse) {
    setConfig(result.config)
    setAuditReport(result.audit_report)
    refresh()
  }

  async function handleLoadVault(vaultId: string) {
    const { config: cfg } = await loadVaultEntry(vaultId)
    setConfig(cfg)
    setAuditReport(null)
    refresh()
  }

  function handleScrapeStarted(id: string) {
    setResults(null)
    setRunId(id)
  }

  async function handleRunDone() {
    setResults(await getResults())
    refresh()
  }

  function handleReset() {
    setConfig(null)
    setAuditReport(null)
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
        hasConfig={config !== null}
        ttlHours={ttlHours}
        onTtlChange={setTtlHours}
        onLoadVault={(id) => void handleLoadVault(id)}
        onChanged={refresh}
        onKill={handleReset}
      />
      <main>
        {backendDown && (
          <div className="error-banner">
            Backend no disponible. Levantalo con{' '}
            <code>uvicorn api:app --port 8000 --workers 1</code> y recargá.
          </div>
        )}
        <Header status={status} />
        {results && <StatTiles results={results} />}
        <CommandCenter onParsed={handleParsed} />
        {config && (
          <ValidationPanel
            config={config}
            auditReport={auditReport}
            ttlHours={ttlHours}
            isRunning={runId !== null && results === null}
            onScrapeStarted={handleScrapeStarted}
            onReparse={() => {
              setConfig(null)
              setAuditReport(null)
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
