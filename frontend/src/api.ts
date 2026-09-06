import type { CollectResult, DashboardData, PipelineResult, QueryResult, UploadResult } from './types'

const DEFAULT_BASE_URL = '/api'

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
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
      this.request<DashboardData['papers']>('/papers?limit=500'),
      this.request<DashboardData['clusters']>('/clusters'),
      this.request<DashboardData['trends']>('/trends'),
      this.request<DashboardData['gaps']>('/gaps?limit=100'),
      this.request<DashboardData['reports']>('/reports'),
    ])
    let graph: DashboardData['graph'] = { nodes: [], edges: [] }
    try {
      graph = await this.request<DashboardData['graph']>('/graph?limit=500')
    } catch (error) {
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) throw error
      // Graph exploration is optional for the reports/trends workspace. Keep the
      // rest of the dashboard useful when Neo4j is temporarily unavailable.
    }
    return { papers, clusters, trends, gaps, reports, graph }
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

  runPipeline(generateReports: boolean): Promise<PipelineResult> {
    return this.request<PipelineResult>('/pipeline/run', {
      method: 'POST',
      body: JSON.stringify({ generate_reports: generateReports }),
    })
  }
}
