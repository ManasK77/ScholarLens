import type { CollectResult, CorpusResetResult, DashboardData, PipelineResult, QueryResult, UploadResult } from './types'

const DEFAULT_BASE_URL = '/api'

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** Collection endpoints return a JSON array. Anything else is treated as empty.
 *
 * The backend contract (`routes_backend.py`) always returns a list from
 * `/papers`, `/clusters`, `/trends`, `/gaps` and `/reports`, so this never
 * changes behaviour against a healthy API. It exists so a proxy error page or
 * a mid-deploy shape change degrades one panel to its empty state instead of
 * throwing inside render and blanking the whole dashboard.
 */
function asList<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

export class ScholarLensApi {
  private readonly apiKey: string
  private readonly baseUrl: string

  constructor(apiKey: string, baseUrl = import.meta.env.VITE_API_BASE_URL || DEFAULT_BASE_URL) {
    this.apiKey = apiKey
    this.baseUrl = baseUrl
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const headers = new Headers(init?.headers)
    headers.set('Accept', 'application/json')
    if (this.apiKey) headers.set('X-API-Key', this.apiKey)
    if (init?.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')

    const response = await fetch(`${this.baseUrl}${path}`, { ...init, headers })
    if (!response.ok) {
      let message = `Request failed (${response.status})`
      try {
        const payload = await response.json() as { detail?: string }
        if (payload.detail) message = payload.detail
      } catch {
        // Keep the status-based message for non-JSON errors.
      }
      throw new ApiError(message, response.status)
    }
    return response.json() as Promise<T>
  }

  async dashboard(): Promise<DashboardData> {
    const [papers, clusters, trends, gaps, reports] = await Promise.all([
      this.request<unknown>('/papers?limit=500'),
      this.request<unknown>('/clusters'),
      this.request<unknown>('/trends'),
      this.request<unknown>('/gaps?limit=100'),
      this.request<unknown>('/reports'),
    ])
    let graph: DashboardData['graph'] = { nodes: [], edges: [] }
    try {
      const snapshot = await this.request<Partial<DashboardData['graph']>>('/graph?limit=500')
      graph = {
        nodes: asList<DashboardData['graph']['nodes'][number]>(snapshot?.nodes),
        edges: asList<DashboardData['graph']['edges'][number]>(snapshot?.edges),
      }
    } catch (error) {
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) throw error
      // Graph exploration is optional for the reports/trends workspace. Keep the
      // rest of the dashboard useful when Neo4j is temporarily unavailable.
    }
    return {
      papers: asList<DashboardData['papers'][number]>(papers),
      clusters: asList<DashboardData['clusters'][number]>(clusters),
      trends: asList<DashboardData['trends'][number]>(trends),
      gaps: asList<DashboardData['gaps'][number]>(gaps),
      reports: asList<DashboardData['reports'][number]>(reports),
      graph,
    }
  }

  query(question: string): Promise<QueryResult> {
    return this.request<QueryResult>('/query', {
      method: 'POST',
      body: JSON.stringify({ question }),
    })
  }

  collect(topic: string): Promise<CollectResult> {
    return this.request<CollectResult>('/collect', {
      method: 'POST',
      body: JSON.stringify({ topic }),
    })
  }

  upload(files: File[]): Promise<UploadResult> {
    const body = new FormData()
    files.forEach((file) => body.append('files', file))
    return this.request<UploadResult>('/upload', { method: 'POST', body })
  }

  /** Wipe the stored corpus via the backend's existing admin endpoint.
   *
   *  Destructive and irreversible: `DELETE /admin/corpus` removes every paper
   *  and everything derived from it (see `app.core.corpus_reset`). The UI must
   *  confirm with the user before calling this. */
  deleteCorpus(): Promise<CorpusResetResult> {
    return this.request<CorpusResetResult>('/admin/corpus', { method: 'DELETE' })
  }

  runPipeline(generateReports: boolean): Promise<PipelineResult> {
    return this.request<PipelineResult>('/pipeline/run', {
      method: 'POST',
      body: JSON.stringify({ generate_reports: generateReports }),
    })
  }
}
