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
}

export interface Cluster {
  id: number
  label: string | null
  member_count: number
  contradiction_flag: boolean
  contradiction_score: number
  members: Evidence[]
}

export interface Trend {
  cluster_id: number
  yearly_counts: Record<string, number>
  slope: number
}

export interface GapRanking {
  cluster_id: number
  score: number
  signals: {
    mention_frequency: number
    trend_slope: number
    solution_density: number
    citation_impact: number
    contradiction_flag: number
  }
}

export interface GapReport {
  cluster_id: number
  gap: string
  evidence: Evidence[]
  confidence: number
  suggested_directions: string[]
  provider: string
  model: string
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
}

export interface QueryResult {
  answer: string
  claims: Array<{ text: string; evidence_ids: string[] }>
  citations: Evidence[]
  grounded: boolean
  retry_count: number
  trace: string[]
  provider: string
  model: string
}

export interface CollectResult {
  count: number
  ids: number[]
}

export interface UploadResult {
  created: number[]
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
}

export interface DashboardData {
  papers: Paper[]
  clusters: Cluster[]
  trends: Trend[]
  gaps: GapRanking[]
  reports: GapReport[]
  graph: GraphSnapshot
}
