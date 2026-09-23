import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import {
  AlertCircle,
  ArrowRight,
  BookOpenText,
  CheckCircle2,
  ChevronDown,
  Database,
  FileUp,
  Globe2,
  KeyRound,
  Layers,
  Network,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  Trash2,
  TrendingUp,
  X,
} from 'lucide-react'
import { ApiError, ScholarLensApi } from './api'
import type { Cluster, CollectDuplicates, DashboardData, Evidence, GapReport, GapRanking, QueryResult } from './types'
import './App.css'

const TrendChart = lazy(() => import('./components/TrendChart'))
const KnowledgeGraph = lazy(() => import('./components/KnowledgeGraph'))

type View = 'reports' | 'library' | 'trends' | 'clusters' | 'graph' | 'ask'

const navItems: Array<{ id: View; label: string; icon: typeof BookOpenText }> = [
  { id: 'reports', label: 'Gap reports', icon: BookOpenText },
  { id: 'library', label: 'Add papers', icon: FileUp },
  { id: 'trends', label: 'Trends', icon: TrendingUp },
  { id: 'clusters', label: 'Clusters', icon: Layers },
  { id: 'graph', label: 'Knowledge graph', icon: Network },
  { id: 'ask', label: 'Ask ScholarLens', icon: Sparkles },
]

const sectionHeadings: Partial<Record<View, { eyebrow: string; title: string }>> = {
  reports: { eyebrow: 'Mode A', title: 'Ranked research gaps' },
  trends: { eyebrow: 'Longitudinal analysis', title: 'Evidence trends over time' },
  clusters: { eyebrow: 'Semantic clustering', title: 'Limitations grouped by meaning' },
  graph: { eyebrow: 'Neo4j exploration', title: 'Paper–entity relationships' },
}

/** The five Stage 8 ranking signals, in the order the gap-score formula reads them. */
const signalLabels: Array<{ key: keyof GapRanking['signals']; label: string }> = [
  { key: 'mention_frequency', label: 'Mentioning papers' },
  { key: 'trend_slope', label: 'Trend slope' },
  { key: 'solution_density', label: 'Solution density' },
  { key: 'citation_impact', label: 'Citation impact' },
  { key: 'contradiction_flag', label: 'Contradiction' },
]

function formatScore(value: number) {
  return Number.isFinite(value) ? value.toFixed(2) : '—'
}

/** Raw signal values, shown as the backend stores them — counts stay whole. */
function formatSignal(value: number) {
  if (!Number.isFinite(value)) return '—'
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

/** Render one evidence chunk's attribution. The API exposes no URL for a chunk,
 *  so this is plain text: a title when the corpus still has the paper, its id
 *  otherwise, plus the publication year when one is known. */
function evidenceLabel(item: Evidence) {
  return `${item.title || `Paper ${item.paper_id}`}${item.year ? ` · ${item.year}` : ''}`
}

function connectionMessage(reason: unknown) {
  if (reason instanceof ApiError && (reason.status === 401 || reason.status === 403)) {
    return 'The API key was rejected. Check your local .env and reconnect.'
  }
  return reason instanceof Error ? reason.message : 'Could not reach the local ScholarLens API.'
}

function ConnectionPanel({ currentKey, onSave, onClose }: { currentKey: string; onSave: (key: string) => void; onClose: () => void }) {
  const [value, setValue] = useState(currentKey)
  const inputRef = useRef<HTMLInputElement>(null)

  // `aria-modal` promises the rest of the page is inert, so the dialog has to
  // behave like one: it takes focus on open, Escape dismisses it, and focus
  // returns to whatever opened it.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    inputRef.current?.focus()
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      opener?.focus?.()
    }
  }, [onClose])

  return (
    <div className="connection-panel" role="dialog" aria-modal="true" aria-labelledby="connection-title">
      <div className="connection-card">
        <button className="icon-button close-button" type="button" onClick={onClose} aria-label="Close API settings"><X size={19} /></button>
        <span className="panel-icon"><KeyRound size={22} /></span>
        <p className="eyebrow">Local connection</p>
        <h2 id="connection-title">Connect to ScholarLens</h2>
        <p className="muted-copy">Use the same API key configured in your local <code>.env</code>. It stays in this browser tab and is never written to the project.</p>
        <form onSubmit={(event) => { event.preventDefault(); onSave(value.trim()) }}>
          <label htmlFor="api-key">API key</label>
          <input ref={inputRef} id="api-key" type="password" value={value} onChange={(event) => setValue(event.target.value)} placeholder="Enter X-API-Key" autoComplete="off" />
          <button className="primary-button full-button" type="submit">Save and reconnect <ArrowRight size={17} /></button>
        </form>
      </div>
    </div>
  )
}

function EmptyState({ title, children }: { title: string; children: string }) {
  return (
    <div className="empty-state">
      <span className="empty-icon"><Search size={24} /></span>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  )
}

function ReportsView({ data }: { data: DashboardData }) {
  const reportByCluster = useMemo(() => new Map(data.reports.filter((report) => !report.stale).map((report) => [report.cluster_id, report])), [data.reports])
  const ranked = data.gaps.map((gap) => ({ gap, report: reportByCluster.get(gap.cluster_id) }))

  if (!ranked.length && !data.reports.length) {
    return <EmptyState title="No ranked gaps yet">Run the backend pipeline after collecting papers. Evidence-backed reports will appear here automatically.</EmptyState>
  }

  const items = ranked.length ? [...ranked, ...data.reports.filter((report) => report.stale).map((report) => ({ report, gap: undefined }))] : data.reports.map((report) => ({ report, gap: undefined }))
  return (
    <div className="report-list">
      {items.map(({ gap, report }, index) => (
        <ReportCard key={`${report?.snapshot_id ?? 'current'}:${report?.cluster_id ?? gap?.cluster_id}`} rank={index + 1} gap={gap} report={report} />
      ))}
    </div>
  )
}

function ReportCard({ report, gap, rank }: { report?: GapReport; gap?: GapRanking; rank: number }) {
  const [expanded, setExpanded] = useState(rank === 1)
  const title = report?.gap ?? `Cluster ${gap?.cluster_id}: explanation pending`
  return (
    <article className="report-card">
      <div className="report-rank">{String(rank).padStart(2, '0')}</div>
      <div className="report-content">
        <div className="report-meta">
          {report?.stale && <span className="contradiction-pill">Previous report — refresh pending</span>}
          {gap && <span className="score-pill">Gap score {formatScore(gap.score)}</span>}
          {report && <span className="confidence-pill"><CheckCircle2 size={13} /> {Math.round(report.confidence * 100)}% confidence</span>}
          {gap?.signals.contradiction_flag ? <span className="contradiction-pill">Conflicting evidence</span> : null}
        </div>
        <h3>{title}</h3>
        {report ? (
          <div className="directions">
            {(report.suggested_directions ?? []).map((direction) => <span key={direction}>{direction}</span>)}
          </div>
        ) : <p className="subtle">Generate Mode A reports to attach a grounded explanation and research directions.</p>}
        {(report || gap) && (
          <>
            <button className="text-button" type="button" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
              {expanded ? 'Hide' : 'View'} {report ? 'supporting evidence' : 'score breakdown'} <ChevronDown className={expanded ? 'rotate' : ''} size={17} />
            </button>
            {expanded && (
              <>
                {gap && <SignalBreakdown signals={gap.signals} />}
                {report && (
                  <div className="evidence-list">
                    {(report.evidence ?? []).length ? (report.evidence ?? []).map((item, evidenceIndex) => (
                      <blockquote key={item.id ?? item.point_id ?? evidenceIndex}>
                        <p>{item.text}</p>
                        <cite>{evidenceLabel(item)}</cite>
                      </blockquote>
                    )) : <p className="subtle">No evidence payload is attached to this stored report.</p>}
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>
    </article>
  )
}

/** The Stage 8 formula's inputs, so a researcher can see *why* a gap ranks where
 *  it does rather than trusting an opaque score. Values are the backend's own —
 *  nothing here is normalised or re-weighted in the UI. */
function SignalBreakdown({ signals }: { signals: GapRanking['signals'] }) {
  return (
    <dl className="signal-grid">
      {signalLabels.map(({ key, label }) => {
        const value = signals?.[key]
        return (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{key === 'contradiction_flag' ? (value ? 'Yes' : 'No') : formatSignal(value as number)}</dd>
          </div>
        )
      })}
    </dl>
  )
}

function ClustersView({ data }: { data: DashboardData }) {
  const sorted = useMemo(() => [...data.clusters].sort((a, b) => b.member_count - a.member_count), [data.clusters])

  if (!sorted.length) {
    return <EmptyState title="No clusters yet">Build the evidence base after collecting papers. Limitations that mean the same thing are grouped here, however differently each author phrased them.</EmptyState>
  }

  return <div className="report-list">{sorted.map((cluster, index) => <ClusterCard key={cluster.id} cluster={cluster} rank={index + 1} />)}</div>
}

function ClusterCard({ cluster, rank }: { cluster: Cluster; rank: number }) {
  const [expanded, setExpanded] = useState(rank === 1)
  const members = cluster.members ?? []
  return (
    <article className="report-card">
      <div className="report-rank">{String(rank).padStart(2, '0')}</div>
      <div className="report-content">
        <div className="report-meta">
          <span className="score-pill">{cluster.member_count} {cluster.member_count === 1 ? 'statement' : 'statements'}</span>
          {cluster.contradiction_flag ? <span className="contradiction-pill">Conflicting evidence</span> : null}
        </div>
        <h3>{cluster.label || `Cluster ${cluster.id}`}</h3>
        <button className="text-button" type="button" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
          {expanded ? 'Hide' : 'View'} clustered statements <ChevronDown className={expanded ? 'rotate' : ''} size={17} />
        </button>
        {expanded && (
          <div className="evidence-list">
            {members.length ? members.map((member, memberIndex) => (
              <blockquote key={member.point_id ?? member.id ?? memberIndex}>
                <p>{member.text}</p>
                <cite>{evidenceLabel(member)}</cite>
              </blockquote>
            )) : <p className="subtle">This cluster's member statements are not stored with it.</p>}
          </div>
        )}
      </div>
    </article>
  )
}

function TrendsView({ data }: { data: DashboardData }) {
  const clusterNames = new Map(data.clusters.map((cluster) => [cluster.id, cluster.label || `Cluster ${cluster.id}`]))
  const sorted = [...data.trends].sort((a, b) => b.slope - a.slope)
  return (
    <div className="split-workspace">
      <div className="chart-panel">
        <Suspense fallback={<div className="loading-block">Preparing trend chart…</div>}>
          <TrendChart trends={data.trends} clusters={data.clusters} />
        </Suspense>
      </div>
      <aside className="trend-ranking">
        <p className="eyebrow">Fastest rising</p>
        <h3>Momentum by cluster</h3>
        {sorted.length ? sorted.slice(0, 8).map((trend, index) => (
          <div className="trend-row" key={trend.cluster_id}>
            <span>{index + 1}</span>
            <div><strong>{clusterNames.get(trend.cluster_id)}</strong><small>{Object.values(trend.yearly_counts ?? {}).reduce((sum, count) => sum + count, 0)} mentions</small></div>
            <b className={trend.slope >= 0 ? 'positive' : 'negative'}>{trend.slope >= 0 ? '+' : ''}{trend.slope.toFixed(2)}</b>
          </div>
        )) : <p className="subtle">No trend scores are available.</p>}
      </aside>
    </div>
  )
}

function GraphView({ data }: { data: DashboardData }) {
  return (
    <Suspense fallback={<div className="loading-block">Arranging the knowledge graph…</div>}>
      <KnowledgeGraph graph={data.graph} />
    </Suspense>
  )
}

/** The duplicate breakdown in words, or null when the API reported none.
 *
 *  Without it a search that found 30 candidates and stored 22 looks like eight
 *  papers went missing. Each category is counted per paper, so the parts always
 *  add up to the total the backend reports. */
function duplicateSummary(duplicates?: CollectDuplicates) {
  if (!duplicates?.total) return null
  const parts = [
    duplicates.cross_source ? `${duplicates.cross_source} across sources` : '',
    duplicates.already_in_corpus ? `${duplicates.already_in_corpus} already in the corpus` : '',
    duplicates.within_source ? `${duplicates.within_source} repeated by one source` : '',
  ].filter(Boolean)
  return `${duplicates.total} ${duplicates.total === 1 ? 'duplicate' : 'duplicates'}${parts.length ? ` (${parts.join(', ')})` : ''}`
}

function LibraryView({ api, data, onRefresh }: { api: ScholarLensApi; data: DashboardData | null; onRefresh: () => Promise<void> }) {
  const [topic, setTopic] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [collecting, setCollecting] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [building, setBuilding] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [generateReports, setGenerateReports] = useState(true)
  const [collectStatus, setCollectStatus] = useState<string | null>(null)
  const [collectDetail, setCollectDetail] = useState<string | null>(null)
  const [uploadStatus, setUploadStatus] = useState<string | null>(null)
  const [pipelineStatus, setPipelineStatus] = useState<string | null>(null)
  const [pipelineWarning, setPipelineWarning] = useState<string | null>(null)
  const [deleteStatus, setDeleteStatus] = useState<string | null>(null)
  const [operationError, setOperationError] = useState<string | null>(null)
  // The ids the last collection/upload touched. `null` means no operation has
  // run in this session yet, and the library falls back to showing the corpus.
  const [lastOperationIds, setLastOperationIds] = useState<number[] | null>(null)

  async function collect(event: FormEvent) {
    event.preventDefault()
    if (topic.trim().length < 2) return
    setCollecting(true)
    setCollectStatus(null)
    setCollectDetail(null)
    setOperationError(null)
    try {
      const result = await api.collect(topic.trim())
      const unreachable = Object.entries(result.sources ?? {}).filter(([, status]) => status !== 'ok').map(([name]) => name)
      // `added` counts rows created; everything else in `count` is a paper the
      // corpus already had and this run refreshed. An API that reports no
      // `added` keeps the original combined wording.
      const updated = result.added === undefined ? 0 : result.count - result.added
      setCollectStatus(
        (result.added === undefined
          ? `${result.count} ${result.count === 1 ? 'paper' : 'papers'} added or updated`
          : `${result.added} ${result.added === 1 ? 'paper' : 'papers'} added` +
            (updated > 0 ? ` · ${updated} existing ${updated === 1 ? 'paper' : 'papers'} updated` : '')) +
          (unreachable.length ? ` · ${unreachable.join(', ')} unreachable this run` : ''),
      )
      setCollectDetail(duplicateSummary(result.duplicates))
      setDeleteStatus(null)
      setLastOperationIds(result.ids ?? [])
      await onRefresh()
    } catch (reason) {
      setOperationError(reason instanceof Error ? reason.message : 'Paper discovery failed.')
    } finally {
      setCollecting(false)
    }
  }

  async function deleteCorpus() {
    setDeleting(true)
    setOperationError(null)
    try {
      await api.deleteCorpus()
      // Only after the backend confirms: the UI must never show an empty
      // corpus the server still holds.
      setConfirmingDelete(false)
      setLastOperationIds(null)
      setCollectStatus(null)
      setCollectDetail(null)
      setUploadStatus(null)
      setPipelineStatus(null)
      setPipelineWarning(null)
      setDeleteStatus('Corpus deleted successfully.')
      await onRefresh()
    } catch (reason) {
      setConfirmingDelete(false)
      setDeleteStatus(null)
      setOperationError(reason instanceof Error ? reason.message : 'The corpus could not be deleted.')
    } finally {
      setDeleting(false)
    }
  }

  function chooseFiles(event: ChangeEvent<HTMLInputElement>) {
    setFiles(Array.from(event.target.files ?? []))
    setUploadStatus(null)
  }

  async function upload(event: FormEvent) {
    event.preventDefault()
    if (!files.length) return
    setUploading(true)
    setUploadStatus(null)
    setDeleteStatus(null)
    setOperationError(null)
    try {
      const result = await api.upload(files)
      setUploadStatus(`${result.created.length} ${result.created.length === 1 ? 'PDF' : 'PDFs'} uploaded`)
      setLastOperationIds(result.created)
      setFiles([])
      await onRefresh()
    } catch (reason) {
      setOperationError(reason instanceof Error ? reason.message : 'PDF upload failed.')
    } finally {
      setUploading(false)
    }
  }

  async function buildEvidence() {
    setBuilding(true)
    setPipelineStatus(null)
    setPipelineWarning(null)
    setOperationError(null)
    try {
      const result = await api.runPipeline(generateReports)
      setPipelineStatus(`Evidence base ready · ${result.indexed} passages indexed`)
      const missingCount = result.missing_pdfs?.length ?? 0
      if (missingCount) {
        setPipelineWarning(`${missingCount} missing ${missingCount === 1 ? 'PDF was' : 'PDFs were'} skipped. Fetch or upload ${missingCount === 1 ? 'it' : 'them'} again.`)
      }
      await onRefresh()
    } catch (reason) {
      setOperationError(reason instanceof Error ? reason.message : 'The evidence pipeline failed.')
    } finally {
      setBuilding(false)
    }
  }

  // The papers of the *current* operation, in full — never a preview slice. A
  // duplicate several sources returned appears once, because the API reports
  // one id per distinct paper. With no operation yet, the section falls back to
  // the loaded corpus, newest first. The list scrolls rather than truncating.
  const papersById = useMemo(() => new Map((data?.papers ?? []).map((paper) => [paper.id, paper])), [data])
  const recentPapers = lastOperationIds
    ? [...lastOperationIds].sort((a, b) => a - b).map((id) => papersById.get(id)).filter((paper) => paper !== undefined)
    : [...(data?.papers ?? [])].reverse()
  // `/papers` returns a bounded window of the corpus, so a very large corpus can
  // leave one of this run's papers outside it. Say so rather than quietly
  // showing fewer papers than the search reported.
  const outsideWindow = (lastOperationIds?.length ?? 0) - recentPapers.length

  return (
    <div className="library-workspace">
      <div className="library-heading">
        <div><p className="eyebrow">Corpus builder</p><h2>Bring the literature in.</h2><p>Find open-access research automatically, upload papers you already have, then turn them into searchable evidence.</p></div>
        <span className="corpus-count"><Database size={16} />{data?.papers.length ?? 0} papers in corpus</span>
      </div>

      {operationError && <div className="error-banner"><AlertCircle size={18} /><span>{operationError}</span></div>}

      <div className="ingestion-grid">
        <article className="ingestion-card">
          <span className="step-number">01</span><Globe2 className="ingestion-icon" size={23} />
          <h3>Find papers for me</h3>
          <p>Searches arXiv, Semantic Scholar, and ACL Anthology, removes duplicates, and retrieves legal open-access PDFs when available.</p>
          <form onSubmit={collect}>
            <label htmlFor="research-topic">Research topic</label>
            <div className="inline-action"><input id="research-topic" value={topic} onChange={(event) => setTopic(event.target.value)} placeholder="e.g. multilingual prompt injection" /><button className="primary-button" type="submit" disabled={collecting || topic.trim().length < 2}>{collecting ? <RefreshCw className="spin" size={17} /> : <Search size={17} />}{collecting ? 'Finding papers…' : 'Find papers'}</button></div>
          </form>
          <button className="danger-button full-button" type="button" onClick={() => setConfirmingDelete(true)} disabled={deleting}>
            {deleting ? <RefreshCw className="spin" size={17} /> : <Trash2 size={17} />}{deleting ? 'Deleting corpus…' : 'Delete corpus'}
          </button>
          {collectStatus && <p className="success-message"><CheckCircle2 size={16} />{collectStatus}</p>}
          {collectDetail && <p className="subtle">{collectDetail}</p>}
          {deleteStatus && <p className="success-message"><CheckCircle2 size={16} />{deleteStatus}</p>}
        </article>

        <article className="ingestion-card">
          <span className="step-number">02</span><FileUp className="ingestion-icon" size={23} />
          <h3>Upload your PDFs</h3>
          <p>Add one or several papers from your computer. ScholarLens extracts their metadata and treats them exactly like discovered papers.</p>
          <form onSubmit={upload}>
            <input className="visually-hidden" id="paper-files" type="file" accept="application/pdf,.pdf" multiple onChange={chooseFiles} />
            <label className="file-picker" htmlFor="paper-files"><FileUp size={20} /><span><strong>Choose PDF files</strong><small>{files.length ? files.map((file) => file.name).join(', ') : 'PDF only · up to the configured size limit'}</small></span></label>
            <button className="secondary-button full-button" type="submit" disabled={uploading || files.length === 0}>{uploading ? <RefreshCw className="spin" size={17} /> : <FileUp size={17} />}{uploading ? 'Uploading PDFs…' : files.length ? `Upload ${files.length} ${files.length === 1 ? 'PDF' : 'PDFs'}` : 'Select PDFs to upload'}</button>
          </form>
          {uploadStatus && <p className="success-message"><CheckCircle2 size={16} />{uploadStatus}</p>}
        </article>
      </div>

      <article className="pipeline-card">
        <div><span className="step-number">03</span><p className="eyebrow">Process the corpus</p><h3>Build the evidence base</h3><p>Parse documents, extract scientific claims, build the knowledge graph, index passages, cluster limitations, and rank research gaps.</p></div>
        <div className="pipeline-actions"><label className="checkbox-label"><input type="checkbox" checked={generateReports} onChange={(event) => setGenerateReports(event.target.checked)} />Generate grounded gap reports with the LLM</label><button className="primary-button" type="button" onClick={() => void buildEvidence()} disabled={building || (data?.papers.length ?? 0) === 0}>{building ? <RefreshCw className="spin" size={17} /> : <Database size={17} />}{building ? 'Building evidence base…' : 'Build evidence base'}</button>{(data?.papers.length ?? 0) === 0 && <small>Add at least one paper first.</small>}{pipelineStatus && <p className="success-message"><CheckCircle2 size={16} />{pipelineStatus}</p>}{pipelineWarning && <p className="warning-message"><AlertCircle size={16} />{pipelineWarning}</p>}</div>
      </article>

      <section className="corpus-list" aria-labelledby="corpus-list-title">
        <div className="corpus-list-heading"><div><p className="eyebrow">Local library</p><h3 id="corpus-list-title">Recently added papers</h3></div>{recentPapers.length > 0 && <span className="quiet-pill">{recentPapers.length} {recentPapers.length === 1 ? 'paper' : 'papers'}{lastOperationIds ? ' from this search' : ' in corpus'}</span>}</div>
        {recentPapers.length ? (
          <div className="corpus-scroll">
            {recentPapers.map((paper) => <article key={paper.id}><span className="paper-source">{paper.source.replaceAll('_', ' ')}</span><div><strong>{paper.title || `Uploaded paper #${paper.id}`}</strong><small>{[paper.authors?.join(', '), paper.venue, paper.year].filter(Boolean).join(' · ') || 'Metadata will be extracted during processing'}</small></div><span className={paper.has_full_text ? 'full-text' : 'metadata-only'}>{paper.has_full_text ? 'Full text' : 'Metadata only'}</span></article>)}
          </div>
        ) : <EmptyState title="Your corpus is empty">Search a topic above or upload PDFs to begin.</EmptyState>}
        {outsideWindow > 0 && <p className="subtle">{outsideWindow} more {outsideWindow === 1 ? 'paper is' : 'papers are'} stored but outside the loaded corpus window.</p>}
      </section>
      {confirmingDelete && (
        <ConfirmDeleteDialog
          busy={deleting}
          onCancel={() => setConfirmingDelete(false)}
          onConfirm={() => void deleteCorpus()}
        />
      )}
    </div>
  )
}

/** The mandatory confirmation for an irreversible, whole-corpus deletion.
 *
 *  A real dialog rather than `window.confirm`, matching `ConnectionPanel`: it
 *  takes focus on open, Escape and No dismiss it without calling the backend,
 *  and focus returns to the button that opened it. "No" is focused first so a
 *  stray Enter cannot delete the corpus. */
function ConfirmDeleteDialog({ busy, onCancel, onConfirm }: { busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  const declineRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    declineRef.current?.focus()
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      opener?.focus?.()
    }
  }, [onCancel])

  return (
    <div className="connection-panel" role="dialog" aria-modal="true" aria-labelledby="delete-corpus-title">
      <div className="connection-card">
        <span className="panel-icon danger-icon"><AlertCircle size={22} /></span>
        <p className="eyebrow">Destructive action</p>
        <h2 id="delete-corpus-title">Are you sure you want to delete the corpus?</h2>
        <p className="muted-copy">This will remove all the stored papers.</p>
        <div className="dialog-actions">
          <button ref={declineRef} className="secondary-button" type="button" onClick={onCancel} disabled={busy}>No</button>
          <button className="danger-button" type="button" onClick={onConfirm} disabled={busy}>
            {busy ? <RefreshCw className="spin" size={17} /> : null}{busy ? 'Deleting…' : 'Yes'}
          </button>
        </div>
      </div>
    </div>
  )
}

interface Exchange {
  id: number
  question: string
  result: QueryResult
}

function AskView({ api, corpusSize }: { api: ScholarLensApi; corpusSize: number }) {
  const [question, setQuestion] = useState('')
  const [exchanges, setExchanges] = useState<Exchange[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    const asked = question.trim()
    if (asked.length < 3) return
    setLoading(true)
    setError(null)
    try {
      const result = await api.query(asked)
      // `/query` is stateless — each question is answered on its own. The
      // transcript is a frontend record so earlier answers and their citations
      // stay readable, not a conversation the backend carries any context for.
      setExchanges((previous) => [...previous, { id: previous.length, question: asked, result }])
      setQuestion('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The query could not be completed.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="ask-layout">
      <div className="ask-intro">
        <p className="eyebrow">Mode B · GraphRAG</p>
        <h2>Ask the evidence graph.</h2>
        <p>ScholarLens searches semantic passages and their graph neighbors, then verifies every generated claim against its cited evidence.</p>
        <div className="prompt-examples">
          {['What remains unsolved in multilingual prompt injection?', 'Which datasets are underexplored?', 'Where do recent findings contradict each other?'].map((prompt) => (
            <button key={prompt} type="button" onClick={() => setQuestion(prompt)}>{prompt}<ArrowRight size={15} /></button>
          ))}
        </div>
      </div>
      <div className="query-panel">
        {corpusSize === 0 && (
          <div className="corpus-warning" role="status">
            <AlertCircle size={18} />
            <span><strong>No papers are indexed yet.</strong> Collect papers and run the evidence pipeline before asking corpus questions.</span>
          </div>
        )}
        <form className="query-form" onSubmit={submit}>
          <label htmlFor="research-question">Research question</label>
          <textarea id="research-question" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="Ask a focused question about your literature corpus…" rows={4} />
          <button className="primary-button" type="submit" disabled={loading || question.trim().length < 3}>
            {loading ? <RefreshCw className="spin" size={17} /> : <Send size={17} />}{loading ? 'Tracing evidence…' : 'Analyze evidence'}
          </button>
        </form>
        {error && <div className="error-banner"><AlertCircle size={18} /><span>{error}</span></div>}
        {exchanges.length > 0 && (
          <div className="ask-thread" aria-live="polite">
            {exchanges.map((exchange) => <AnswerCard key={exchange.id} exchange={exchange} />)}
          </div>
        )}
      </div>
    </div>
  )
}

function AnswerCard({ exchange: { question, result } }: { exchange: Exchange }) {
  const citations = result.citations ?? []
  return (
    <article className="answer-card">
      <p className="asked-question">{question}</p>
      <div className="answer-status">
        <span className={result.grounded ? 'grounded' : 'ungrounded'}>{result.grounded ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}{result.grounded ? 'Grounded answer' : citations.length === 0 ? 'No indexed evidence' : 'Evidence incomplete'}</span>
        <small>{result.retry_count ? `${result.retry_count} retrieval retry` : 'First-pass retrieval'} · {result.model}</small>
      </div>
      <p className="answer-text">{result.answer}</p>
      {citations.length > 0 && <div className="citation-list"><h3>Sources</h3>{citations.map((citation, index) => (
        <div className="citation" key={citation.id ?? citation.point_id ?? index}>
          <span>{index + 1}</span>
          <div><strong>{citation.title || `Paper ${citation.paper_id}`}</strong>{citation.year ? <small>{citation.year}</small> : null}<p>{citation.text}</p></div>
        </div>
      ))}</div>}
    </article>
  )
}

function App() {
  const [view, setView] = useState<View>('reports')
  const [apiKey, setApiKey] = useState(() => sessionStorage.getItem('scholarlens-api-key') || '')
  const [showConnection, setShowConnection] = useState(false)
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const api = useMemo(() => new ScholarLensApi(apiKey), [apiKey])

  const loadDashboard = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await api.dashboard())
    } catch (reason) {
      setError(connectionMessage(reason))
    } finally {
      setLoading(false)
    }
  }, [api])

  useEffect(() => {
    let active = true
    api.dashboard().then((nextData) => {
      if (active) setData(nextData)
    }).catch((reason: unknown) => {
      if (active) setError(connectionMessage(reason))
    }).finally(() => {
      if (active) setLoading(false)
    })
    return () => { active = false }
  }, [api])

  function saveApiKey(key: string) {
    sessionStorage.setItem('scholarlens-api-key', key)
    setApiKey(key)
    setShowConnection(false)
  }

  // Stable identity: the dialog's focus/Escape effect keys off this handler, and
  // a fresh closure each render would tear that effect down mid-interaction.
  const closeConnection = useCallback(() => setShowConnection(false), [])

  const pageTitle = { reports: 'Evidence, not guesswork.', library: 'Build your research corpus.', trends: 'See where the field is moving.', clusters: 'One gap, many phrasings.', graph: 'Trace the research landscape.', ask: 'Question the corpus directly.' }[view]
  const reportsCount = data?.reports.length ?? 0

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button className="brand" type="button" onClick={() => setView('reports')} aria-label="ScholarLens home"><span className="brand-mark"><Search size={18} /></span><span>ScholarLens</span></button>
        <nav aria-label="Primary navigation">
          {navItems.map((item) => { const Icon = item.icon; return <button key={item.id} className={`nav-item ${view === item.id ? 'active' : ''}`} type="button" onClick={() => setView(item.id)}><Icon size={18} />{item.label}</button> })}
        </nav>
        <div className="sidebar-note"><span className={`status-dot ${error ? 'offline' : ''}`} />{error ? 'API needs attention' : loading ? 'Reading evidence base' : 'Evidence pipeline connected'}</div>
      </aside>

      <main id="top">
        <header className="topbar">
          <div><p className="eyebrow">Research intelligence workspace</p><h1>{pageTitle}</h1></div>
          <div className="header-actions"><button className="icon-button" type="button" onClick={() => void loadDashboard()} aria-label="Refresh dashboard"><RefreshCw className={loading ? 'spin' : ''} size={18} /></button><button className="secondary-button" type="button" onClick={() => setShowConnection(true)}><KeyRound size={16} /> Connect API</button></div>
        </header>

        {view === 'reports' && <section className="overview-grid" aria-label="Corpus overview">
          <article className="metric-card featured"><span>Ranked reports</span><strong>{loading ? '·' : reportsCount}</strong><p>Grounded explanations ready to investigate.</p></article>
          <article className="metric-card"><span>Papers loaded</span><strong>{loading ? '·' : data?.papers.length ?? 0}</strong><p>Visible in the normalized corpus window.</p></article>
          <article className="metric-card"><span>Evidence clusters</span><strong>{loading ? '·' : data?.clusters.length ?? 0}</strong><p>Related limitations and future work.</p></article>
        </section>}

        <section className={`workspace-card ${view === 'ask' ? 'dark-workspace' : ''}`}>
          {sectionHeadings[view] && <div className="section-heading"><div><p className="eyebrow">{sectionHeadings[view]?.eyebrow}</p><h2>{sectionHeadings[view]?.title}</h2></div>{data && <span className="quiet-pill">Updated from local API</span>}</div>}
          {loading && <div className="loading-block"><RefreshCw className="spin" size={22} />Loading the evidence base…</div>}
          {!loading && error && <div className="error-state"><AlertCircle size={28} /><h3>Connection needed</h3><p>{error}</p><button className="primary-button" type="button" onClick={() => setShowConnection(true)}>Open API settings</button></div>}
          {!loading && !error && data && view === 'reports' && <ReportsView data={data} />}
          {!loading && !error && data && view === 'trends' && <TrendsView data={data} />}
          {!loading && !error && data && view === 'clusters' && <ClustersView data={data} />}
          {!loading && !error && data && view === 'graph' && <GraphView data={data} />}
          {!error && <div hidden={view !== 'library' || (loading && !data)}><LibraryView api={api} data={data} onRefresh={loadDashboard} /></div>}
          {!error && <div hidden={view !== 'ask' || (loading && !data)}><AskView api={api} corpusSize={data?.papers.length ?? 0} /></div>}
        </section>
      </main>
      {showConnection && <ConnectionPanel currentKey={apiKey} onSave={saveApiKey} onClose={closeConnection} />}
    </div>
  )
}

export default App
