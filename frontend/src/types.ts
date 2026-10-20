export interface Paper {
  id: number
  title: string | null
  authors: string[] | null
  year: number | null
  venue: string | null
  source: string
  has_full_text: boolean
  citation_count: number | null
}

export interface Evidence {
  id?: string
  point_id?: string
  paper_id: number
  title?: string | null
  text: string
  year?: number | null
  source_type?: string
  /** Every label one sentence is stored under (cluster members only). */
  source_types?: string[]
  point_ids?: string[]
  /** Citation markers in the text, e.g. "[12]": the passage may report other work. */
  citation_markers?: string[]
  /** Report evidence only: the quote reached the stored length cap, so the passage may continue. */
  truncated?: boolean
}

/**
 * One verified cross-paper disagreement inside a cluster.
 *
 * The backend stores only pairs that passed Stage 6's claim, topic, direction
 * and corroboration gates, so every entry here is something the "Conflicting
 * evidence" pill can be made to explain rather than merely assert. Optional on
 * `Cluster` so an older API response still type-checks.
 */
export interface ContradictionPair {
  left_point_id: string
  left_paper_id: number
  left_text: string | null
  right_point_id: string
  right_paper_id: number
  right_text: string | null
  score: number
}

export interface Cluster {
  id: number
  label: string | null
  member_count: number
  paper_count?: number
  evidence_scope?: 'single_paper' | 'corroborated'
  contradiction_flag: boolean
  contradiction_score: number
  members: Evidence[]
  contradictions?: ContradictionPair[]
}

export interface TrendQualifiers {
  papers: number
  years: number
  max_papers_per_year: number
  small_sample: boolean
  partial_years: string[]
  single_year: boolean
}

export interface Trend {
  cluster_id: number
  /** Distinct papers per year (not mentions). */
  yearly_counts: Record<string, number>
  slope: number
  qualifiers?: TrendQualifiers
}

export interface GapRanking {
  cluster_id: number
  score: number
  /** Distinct papers stating the theme; one paper = exploratory. */
  paper_count?: number
  evidence_scope?: 'single_paper' | 'corroborated'
  exploratory?: boolean
  signals: {
    mention_frequency: number
    trend_slope: number
    solution_density: number
    citation_impact: number
    contradiction_flag: number
  }
}

/**
 * One of the three claims a gap statement rests on, with the evidence behind
 * it: (A) the problem exists, (B) existing solutions fall short, (C) a
 * specific research gap remains. `supported` is false unless a retrieved
 * passage actually carried that claim.
 */
export interface GroundedClaim {
  supported: boolean
  statement: string
  evidence_ids: string[]
  /** llm_cited | not_supported | nli_verified | llm_unverified | not_claimed */
  basis?: string
  llm_supported?: boolean
}

export interface ReportJustification {
  problem_evidence?: GroundedClaim
  solution_insufficiency_evidence?: GroundedClaim
  research_gap_evidence?: GroundedClaim
  conflicting_evidence?: GroundedClaim
}

/**
 * How much evidence a report rests on, and from how many papers. Eight
 * passages from one study and eight from eight studies are not equally
 * supported, and only `unique_paper_count` tells them apart.
 */
export interface EvidenceSummary {
  passage_count: number
  unique_paper_count: number
  max_passages_from_one_paper: number
}

export interface GapReport {
  stale?: boolean
  snapshot_id?: string
  snapshot_created_at?: string
  cluster_id: number
  gap: string
  evidence: Evidence[]
  /**
   * LLM-generated and NOT statistically calibrated: it is the model's own
   * estimate, prompted to fall when the evidence is thin, dominated by one
   * paper, or missing support for (B) or (C). Treat it as a hint, not a
   * probability.
   */
  confidence: number
  suggested_directions: string[]
  provider: string
  model: string
  justification?: ReportJustification
  evidence_summary?: EvidenceSummary
}

export interface GraphNode {
  id: string
  labels: string[]
  label: string | null
}

export interface GraphEdge {
  source: string
  target: string
  type: string
}

export interface GraphSnapshot {
  nodes: GraphNode[]
  edges: GraphEdge[]
  /** Size of the whole graph; the snapshot may be a bounded part of it. */
  total_nodes?: number
  total_edges?: number
}

export interface QueryResult {
  /** Which terminal Mode B reached. Absent from an older API. */
  outcome?: 'grounded_answer' | 'no_indexed_evidence' | 'insufficient_evidence' | 'grounding_failed'
  answer: string
  claims: Array<{ text: string; evidence_ids: string[] }>
  citations: Evidence[]
  grounded: boolean
  retry_count: number
  /** Draft claims that failed a check and are not shown (0 when complete). Absent from an older API. */
  removed_claim_count?: number
  trace: string[]
  provider: string
  model: string
}

/** Distinct papers a collection recognised rather than re-created. Categories
 *  are mutually exclusive and counted per paper, so one paper three sources all
 *  returned is a single cross-source duplicate — see `RunLedger` in
 *  `routes_ingestion.py` for the priority between them. */
export interface CollectDuplicates {
  total: number
  cross_source: number
  within_source: number
  already_in_corpus: number
}

export interface CollectResult {
  /** Distinct corpus rows this run touched — inserted, merged, or enriched. */
  count: number
  ids: number[]
  /** Rows actually created. `count - added` were already in the corpus. */
  added?: number
  duplicates?: CollectDuplicates
  /** Per-source outcome: 'ok', or 'failed: <reason>' when that source was unreachable. */
  sources?: Record<string, string>
  /** What each source contributed before dedup, so a source that stored
   *  nothing can be told from a source that returned nothing. */
  source_stats?: Record<string, { candidates: number; added: number; duplicates: number }>
  /** The run's outcome (S1 hardening, 2026-10-12). Anything short of
   *  `completed` comes with `warnings` saying why. */
  status?: 'completed' | 'completed_with_warnings' | 'partial' | 'failed'
  warnings?: CollectWarning[]
  /** Full text vs metadata only, over the distinct papers the run touched. */
  full_text?: { touched: CollectCoverage; added: CollectCoverage }
  collection_run_id?: number
}

/** Why a run was not a complete, clean, exhaustive read. */
export interface CollectWarning {
  source: string
  code: string
  message: string
}

export interface CollectCoverage {
  papers: number
  with_full_text: number
  metadata_only: number
  metadata_only_without_doi: number
  coverage: number | null
}

export interface CorpusResetResult {
  deleted: boolean
  papers: number
  pdfs: number
}

export interface UploadResult {
  created: number[]
}

/** How the run's gap-report step (S9) went. `reports` alone cannot tell
 *  "2 of 2" from "2 of 5". Absent from an older API. */
export interface ReportGeneration {
  status: 'complete' | 'partial' | 'failed' | 'not_requested'
  requested: number
  succeeded: number
  failed: number
  skipped: number
  reused: number
  failures: { cluster_id: number; reason: string }[]
  skips: { cluster_id: number; reason: string }[]
}

export interface PipelineResult {
  parsed: number[]
  missing_pdfs?: number[]
  extracted: number[]
  graphed: number[]
  indexed: number
  clusters: number[]
  trends: number[]
  gaps: number[]
  reports: number[]
  report_generation?: ReportGeneration
}

export interface DashboardData {
  papers: Paper[]
  clusters: Cluster[]
  trends: Trend[]
  gaps: GapRanking[]
  reports: GapReport[]
  graph: GraphSnapshot
}
