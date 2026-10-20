import type { CollectResult, CorpusResetResult, DashboardData, PipelineResult, QueryResult, UploadResult } from './types'

const DEFAULT_BASE_URL = '/api'

export class ApiError extends Error {
  status: number
  /** Seconds to wait before retrying, from a 429's `Retry-After` header. */
  retryAfter?: number

  constructor(message: string, status: number, retryAfter?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.retryAfter = retryAfter
  }
}

/** `Retry-After` in seconds. Only the delay-seconds form is used by the API. */
function retryAfterSeconds(response: Response): number | undefined {
  const header = response.headers.get('Retry-After')?.trim()
  const seconds = header ? Number(header) : NaN
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : undefined
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

/** The message in a FastAPI error body: `detail` is a string for errors the
 *  API raises, and a list of `{msg}` objects for request validation (422). */
function errorDetail(payload: unknown): string | undefined {
  const detail = (payload as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    const first = detail.find((item): item is { msg: string } => typeof item?.msg === 'string')
    if (first) return `Invalid request: ${first.msg}`
  }
  return undefined
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

    let response: Response
    try {
      response = await fetch(`${this.baseUrl}${path}`, { ...init, headers })
    } catch (error) {
      // A caller's abort stays an AbortError so it can tell "cancelled" apart.
      if (error instanceof DOMException && error.name === 'AbortError') throw error
      throw new ApiError('Could not reach the ScholarLens API.', 0)
    }
    if (!response.ok) {
      let message = `Request failed (${response.status})`
      try {
        message = errorDetail(await response.json()) ?? message
      } catch {
        // Keep the status-based message for non-JSON errors.
      }
      throw new ApiError(message, response.status, retryAfterSeconds(response))
    }
    try {
      return await response.json() as T
    } catch {
      // e.g. a proxy's HTML page served with 200: never a raw parser error.
      throw new ApiError('The API returned a response that could not be read.', response.status)
    }
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
        total_nodes: typeof snapshot?.total_nodes === 'number' ? snapshot.total_nodes : undefined,
        total_edges: typeof snapshot?.total_edges === 'number' ? snapshot.total_edges : undefined,
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

  runPipeline(generateReports: boolean, signal?: AbortSignal): Promise<PipelineResult> {
    return this.request<PipelineResult>('/pipeline/run', {
      method: 'POST',
      body: JSON.stringify({ generate_reports: generateReports }),
      signal,
    })
  }
}
