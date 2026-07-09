import { useCallback, useEffect, useState } from 'react'
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
import type {
  CvProfile, ParseResponse, RegistryStats, ResultsResponse, SchedulerState,
  SearchConfigDto, Status, VaultEntry,
} from './types'

export default function App() {
  const [status, setStatus] = useState<Status | null>(null)
  const [stats, setStats] = useState<RegistryStats | null>(null)
  const [vault, setVault] = useState<VaultEntry[]>([])
  const [scheduler, setScheduler] = useState<SchedulerState | null>(null)
  const [profile, setProfile] = useState<CvProfile | null>(null)
  const [config, setConfig] = useState<SearchConfigDto | null>(null)
  const [auditReport, setAuditReport] = useState<string | null>(null)
  const [ttlHours, setTtlHours] = useState(0)
  const [runId, setRunId] = useState<string | null>(null)
  const [results, setResults] = useState<ResultsResponse | null>(null)
  const [backendDown, setBackendDown] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [s, r, v, sch, cv] = await Promise.all([
        getStatus(), getRegistryStats(), getVault(), getScheduler(), getCvProfile(),
      ])
      setStatus(s)
      setStats(r)
      setVault(v.entries)
      setScheduler(sch)
      setProfile(cv.profile)
      setBackendDown(false)
    } catch {
      setBackendDown(true)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    if (status) setTtlHours(status.ttl_hours)
    // solo al primer status — el slider es fuente de verdad después
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status === null])

  function handleParsed(result: ParseResponse) {
    setConfig(result.config)
    setAuditReport(result.audit_report)
    void refresh()
  }

  async function handleLoadVault(vaultId: string) {
    try {
      const { config: cfg } = await loadVaultEntry(vaultId)
      setConfig(cfg)
      setAuditReport(null)
      void refresh()
    } catch {
      setBackendDown(true)
    }
  }

  function handleScrapeStarted(id: string) {
    setResults(null)
    setRunId(id)
  }

  async function handleRunDone() {
    try {
      setResults(await getResults())
    } catch {
      setBackendDown(true)
    }
    void refresh()
  }

  function handleReset() {
    setConfig(null)
    setAuditReport(null)
    setRunId(null)
    setResults(null)
    void refresh()
  }

  if (status?.needs_setup) {
    return <SetupWizard onDone={() => void refresh()} />
  }

  return (
    <div className="app">
      <Sidebar
        status={status}
        stats={stats}
        vault={vault}
        scheduler={scheduler}
        profile={profile}
        hasConfig={config !== null}
        ttlHours={ttlHours}
        onTtlChange={setTtlHours}
        onLoadVault={handleLoadVault}
        onChanged={() => void refresh()}
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
        {runId && <PipelineConsole runId={runId} onDone={() => void handleRunDone()} key={runId} />}
        {results && <Results results={results} />}
      </main>
    </div>
  )
}
