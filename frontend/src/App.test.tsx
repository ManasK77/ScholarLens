import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { liveGroundedAnswer } from './test/liveGroundedAnswer'

const fixtures: Record<string, unknown> = {
  '/api/papers?limit=500': [{ id: 1, title: 'A paper', authors: ['A'], year: 2025, venue: 'ACL', source: 'arxiv', has_full_text: true, citation_count: 12 }],
  '/api/clusters': [{
    id: 4,
    label: 'Multilingual robustness',
    member_count: 7,
    contradiction_flag: true,
    contradiction_score: 0.81,
    members: [
      { point_id: 'p1', paper_id: 1, text: 'Performance decreases for multilingual datasets.', year: 2024 },
      { point_id: 'p2', paper_id: 2, text: 'Existing datasets focus primarily on English.', year: 2023 },
    ],
  }],
  '/api/trends': [{ cluster_id: 4, yearly_counts: { 2023: 2, 2024: 4, 2025: 7 }, slope: 2.5 }],
  '/api/gaps?limit=100': [{ cluster_id: 4, score: 8.4, signals: { mention_frequency: 7, trend_slope: 2.5, solution_density: 0.14, citation_impact: 1.2, contradiction_flag: 1 } }],
  '/api/reports': [{ cluster_id: 4, gap: 'Multilingual prompt injection remains underexplored.', evidence: [{ id: 'e1', paper_id: 1, title: 'A paper', text: 'Methods degrade outside English.' }], confidence: 0.91, suggested_directions: ['Build multilingual benchmarks'], provider: 'groq', model: 'openai/gpt-oss-20b' }],
  '/api/graph?limit=500': { nodes: [], edges: [] },
}

function installFetchMock() {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace('http://localhost:8000', '')
    if (url === '/api/collect' && init?.method === 'POST') {
      return new Response(JSON.stringify({ count: 3, ids: [2, 3, 4] }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url === '/api/upload' && init?.method === 'POST') {
      return new Response(JSON.stringify({ created: [5, 6] }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url === '/api/pipeline/run' && init?.method === 'POST') {
      return new Response(JSON.stringify({ parsed: [2, 3], extracted: [2, 3], graphed: [2, 3], indexed: 18, clusters: [1], trends: [1], gaps: [1], reports: [1] }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    if (url === '/api/query' && init?.method === 'POST') {
      return new Response(JSON.stringify({
        answer: 'Low-resource languages remain under-supported.',
        claims: [{ text: 'Low-resource languages remain under-supported.', evidence_ids: ['e1'] }],
        citations: [{ id: 'e1', paper_id: 1, title: 'A paper', text: 'Methods degrade outside English.', year: 2024 }],
        grounded: true,
        retry_count: 1,
        trace: ['retrieve:0', 'reformulate', 'retrieve:1', 'grounding:true'],
        provider: 'groq',
        model: 'openai/gpt-oss-20b',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    const body = fixtures[url]
    return body === undefined
      ? new Response(JSON.stringify({ detail: 'Not found' }), { status: 404 })
      : new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
}

function queryPayload() {
  return {
    answer: 'Low-resource languages remain under-supported.',
    claims: [{ text: 'Low-resource languages remain under-supported.', evidence_ids: ['e1'] }],
    citations: [{ id: 'e1', paper_id: 1, title: 'A paper', text: 'Methods degrade outside English.' }],
    grounded: true,
    retry_count: 1,
    trace: ['retrieve:0', 'reformulate', 'retrieve:1', 'grounding:true'],
    provider: 'groq',
    model: 'openai/gpt-oss-20b',
  }
}

describe('ScholarLens dashboard', () => {
  beforeEach(() => installFetchMock())
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('renders ranked, evidence-backed reports from the API', async () => {
    render(<App />)

    expect(await screen.findByText('Multilingual prompt injection remains underexplored.')).toBeInTheDocument()
    expect(screen.getByText('91% confidence')).toBeInTheDocument()
    expect(screen.getByText('Build multilingual benchmarks')).toBeInTheDocument()
  })

  it('answers a research question and displays its citation', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /ask scholarlens/i }))
    await user.type(screen.getByLabelText(/research question/i), 'What gaps exist in multilingual safety?')
    await user.click(screen.getByRole('button', { name: /analyze evidence/i }))

    expect(await screen.findByText('Low-resource languages remain under-supported.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('A paper').length).toBeGreaterThan(0))
    // An automated check is not proof: the grounded answer says so.
    expect(screen.getByText(/checked automatically against the cited passages; not verified by a person/i)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Cited passages' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Sources' })).not.toBeInTheDocument()
  })

  it('keeps historical reports separate from a reused cluster id', async () => {
    const original = fixtures['/api/reports']
    fixtures['/api/reports'] = [{ ...(original as object[])[0], stale: true, snapshot_id: 'old-run' }]
    try {
      render(<App />)
      expect(await screen.findByText('Previous report — refresh pending')).toBeInTheDocument()
      expect(screen.getByText('Cluster 4: explanation pending')).toBeInTheDocument()
      expect(screen.getByText('Multilingual prompt injection remains underexplored.')).toBeInTheDocument()
    } finally {
      fixtures['/api/reports'] = original
    }
  })

  it('preserves an in-flight query and its result while switching views', async () => {
    const user = userEvent.setup()
    const fallbackFetch = vi.mocked(fetch)
    let resolveQuery!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/query')) {
        return new Promise<Response>((resolve) => { resolveQuery = resolve })
      }
      return fallbackFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^ask scholarlens$/i }))
    await user.type(screen.getByLabelText(/research question/i), 'What gaps exist in multilingual safety?')
    await user.click(screen.getByRole('button', { name: /analyze evidence/i }))
    await user.click(screen.getByRole('button', { name: /^gap reports$/i }))
    await user.click(screen.getByRole('button', { name: /^ask scholarlens$/i }))

    expect(screen.getByLabelText(/research question/i)).toHaveValue('What gaps exist in multilingual safety?')
    expect(screen.getByRole('button', { name: /tracing evidence/i })).toBeDisabled()

    resolveQuery(new Response(JSON.stringify(queryPayload()), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    expect(await screen.findByText('Low-resource languages remain under-supported.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^trends$/i }))
    await user.click(screen.getByRole('button', { name: /^ask scholarlens$/i }))
    expect(screen.getByText('Low-resource languages remain under-supported.')).toBeInTheDocument()
  })

  it('explains when no evidence is indexed instead of showing a vague status', async () => {
    const user = userEvent.setup()
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/query')) {
        return new Response(JSON.stringify({
          answer: 'No indexed evidence was found for this question. Collect papers and run the evidence pipeline before querying.',
          claims: [],
          citations: [],
          grounded: false,
          retry_count: 2,
          trace: ['retrieve:0', 'insufficient'],
          provider: 'groq',
          model: 'openai/gpt-oss-20b',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return populatedFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^ask scholarlens$/i }))
    await user.type(screen.getByLabelText(/research question/i), 'What gaps exist?')
    await user.click(screen.getByRole('button', { name: /analyze evidence/i }))

    expect(await screen.findByText('No indexed evidence')).toBeInTheDocument()
    expect(screen.queryByText('Evidence incomplete')).not.toBeInTheDocument()
  })

  it('collects papers for a topic from scholarly sources', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    await user.type(screen.getByLabelText(/research topic/i), 'multilingual prompt injection')
    await user.click(screen.getByRole('button', { name: /find papers/i }))

    expect(await screen.findByText('3 papers added or updated')).toBeInTheDocument()
    const request = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/collect'))
    expect(request).toBeDefined()
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({ topic: 'multilingual prompt injection' })
  })

  it('uploads multiple PDFs as multipart form data', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    const files = [
      new File(['paper one'], 'paper-one.pdf', { type: 'application/pdf' }),
      new File(['paper two'], 'paper-two.pdf', { type: 'application/pdf' }),
    ]
    await user.upload(screen.getByLabelText(/choose pdf files/i), files)
    await user.click(screen.getByRole('button', { name: /upload 2 pdfs/i }))

    expect(await screen.findByText('2 PDFs uploaded')).toBeInTheDocument()
    const request = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/upload'))
    expect(request?.[1]?.body).toBeInstanceOf(FormData)
    expect(new Headers(request?.[1]?.headers).has('Content-Type')).toBe(false)
  })

  it('preserves pipeline progress while switching views', async () => {
    const user = userEvent.setup()
    const fallbackFetch = vi.mocked(fetch)
    let resolvePipeline!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/pipeline/run')) {
        return new Promise<Response>((resolve) => { resolvePipeline = resolve })
      }
      return fallbackFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    await user.click(screen.getByRole('button', { name: /build evidence base/i }))
    await user.click(screen.getByRole('button', { name: /^gap reports$/i }))
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))

    expect(screen.getByRole('button', { name: /building evidence base/i })).toBeDisabled()
    resolvePipeline(new Response(JSON.stringify({ parsed: [1], missing_pdfs: [51], extracted: [1], graphed: [1], indexed: 8, clusters: [4], trends: [4], gaps: [4], reports: [4] }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    expect(await screen.findByText('Evidence base ready · 8 passages indexed')).toBeInTheDocument()
    expect(screen.getByText(/1 missing PDF was skipped/i)).toBeInTheDocument()
  })

  it('does not abort an in-flight pipeline request when the dashboard refresh fails', async () => {
    const user = userEvent.setup()
    const fallbackFetch = vi.mocked(fetch)
    let pipelineSignal: AbortSignal | null | undefined
    let resolvePipeline!: (response: Response) => void
    let dashboardDown = false
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/pipeline/run')) {
        pipelineSignal = init?.signal
        return new Promise<Response>((resolve) => { resolvePipeline = resolve })
      }
      if (dashboardDown && url.includes('/papers')) return Promise.reject(new TypeError('Failed to fetch'))
      return fallbackFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    await user.click(screen.getByRole('button', { name: /build evidence base/i }))
    expect(pipelineSignal?.aborted).toBe(false)

    dashboardDown = true
    await user.click(screen.getByRole('button', { name: /refresh dashboard/i }))
    expect(await screen.findByText('Connection needed')).toBeInTheDocument()

    // The dashboard failing is not the user leaving: the build is still running.
    expect(pipelineSignal?.aborted).toBe(false)
    dashboardDown = false
    resolvePipeline(new Response(JSON.stringify({ parsed: [1], extracted: [1], graphed: [1], indexed: 8, clusters: [4], trends: [4], gaps: [4], reports: [4] }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    expect(await screen.findByText('Evidence base ready · 8 passages indexed')).toBeInTheDocument()
    expect(screen.queryByText(/lost contact with the pipeline request/i)).not.toBeInTheDocument()
  })

  it('still aborts the pipeline request when the Library view is really left', async () => {
    const user = userEvent.setup()
    const fallbackFetch = vi.mocked(fetch)
    let pipelineSignal: AbortSignal | null | undefined
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/pipeline/run')) {
        pipelineSignal = init?.signal
        return new Promise<Response>(() => undefined)
      }
      return fallbackFetch(input, init)
    }))
    const { unmount } = render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    await user.click(screen.getByRole('button', { name: /build evidence base/i }))
    expect(pipelineSignal?.aborted).toBe(false)

    unmount()

    expect(pipelineSignal?.aborted).toBe(true)
  })

  it('re-enables the build button when the post-build dashboard refresh never settles', async () => {
    const user = userEvent.setup()
    const fallbackFetch = vi.mocked(fetch)
    let pipelineDone = false
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/pipeline/run')) {
        pipelineDone = true
        return Promise.resolve(new Response(JSON.stringify({ parsed: [1], extracted: [1], graphed: [1], indexed: 8, clusters: [4], trends: [4], gaps: [4], reports: [4] }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      }
      // Once the build has finished, the dashboard refresh hangs forever.
      if (pipelineDone && url.includes('/papers')) return new Promise<Response>(() => undefined)
      return fallbackFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))

    await user.click(screen.getByRole('button', { name: /build evidence base/i }))

    expect(await screen.findByText('Evidence base ready · 8 passages indexed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /build evidence base/i })).toBeEnabled()
  })

  it('re-enables the build button, and says what happened, when the request is lost', async () => {
    const user = userEvent.setup()
    const fallbackFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/pipeline/run')) {
        return Promise.reject(new DOMException('aborted', 'AbortError'))
      }
      return fallbackFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))

    await user.click(screen.getByRole('button', { name: /build evidence base/i }))

    expect(await screen.findByText(/lost contact with the pipeline request/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /build evidence base/i })).toBeEnabled()
  })

  it('sends only one pipeline request while a build is in flight', async () => {
    const user = userEvent.setup()
    const fallbackFetch = vi.mocked(fetch)
    let calls = 0
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/pipeline/run')) { calls += 1; return new Promise<Response>(() => undefined) }
      return fallbackFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))

    await user.click(screen.getByRole('button', { name: /build evidence base/i }))
    await user.click(screen.getByRole('button', { name: /building evidence base/i }))

    expect(calls).toBe(1)
    expect(screen.getByRole('button', { name: /building evidence base/i })).toBeDisabled()
  })

  it('says so when only some of the requested gap reports were generated', async () => {
    const user = userEvent.setup()
    const fallbackFetch = vi.mocked(fetch)
    const reportGeneration = (status: string, succeeded: number, failed: number) => ({
      status, requested: succeeded + failed, succeeded, failed, skipped: 0, reused: 0,
      failures: Array.from({ length: failed }, (_, index) => ({ cluster_id: index + 5, reason: 'Groq\'s structured output did not validate against the report schema' })),
      skips: [],
    })
    let generation = reportGeneration('partial', 2, 3)
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/pipeline/run')) {
        return Promise.resolve(new Response(JSON.stringify({ parsed: [1], extracted: [1], graphed: [1], indexed: 8, clusters: [4], trends: [4], gaps: [4], reports: [4], report_generation: generation }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      }
      return fallbackFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))

    await user.click(screen.getByRole('button', { name: /build evidence base/i }))
    expect(await screen.findByText(/2 of 5 gap reports were generated; 3 failed/i)).toBeInTheDocument()

    generation = reportGeneration('failed', 0, 2)
    await user.click(screen.getByRole('button', { name: /build evidence base/i }))
    expect(await screen.findByText(/no gap reports were generated; 2 failed/i)).toBeInTheDocument()

    generation = reportGeneration('complete', 5, 0)
    await user.click(screen.getByRole('button', { name: /build evidence base/i }))
    await screen.findByText('Evidence base ready · 8 passages indexed')
    expect(screen.queryByText(/gap reports were generated/i)).not.toBeInTheDocument()
  })

  it('breaks the gap score down into the ranking signals the API returns', async () => {
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    // The top-ranked card opens by default, so the breakdown is visible without interaction.
    expect(screen.getByText('Mentioning papers')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
    expect(screen.getByText('Solution density')).toBeInTheDocument()
    expect(screen.getByText('0.14')).toBeInTheDocument()
    expect(screen.getByText('Citation impact')).toBeInTheDocument()
    expect(screen.getByText('1.20')).toBeInTheDocument()
  })

  it('labels the model-written summary and shows a conflict only when it was verified', async () => {
    const original = fixtures['/api/reports']
    const base = (original as Record<string, unknown>[])[0]
    const claim = { supported: false, statement: 'Studies disagree.', evidence_ids: ['e1'], llm_supported: true, basis: 'llm_unverified' }
    fixtures['/api/reports'] = [{ ...base, model: 'openai/gpt-oss-20b', justification: { conflicting_evidence: claim } }]
    try {
      render(<App />)
      expect(await screen.findByText(/Model-written summary \(openai\/gpt-oss-20b\)/)).toBeInTheDocument()
      // The LLM asserted a conflict NLI did not confirm: not presented as one.
      expect(screen.queryByText('Conflict verified by NLI')).not.toBeInTheDocument()
      // The top-ranked card opens expanded, showing its quoted passages.
      expect(screen.getByText('Retrieved passages, quoted verbatim')).toBeInTheDocument()
    } finally {
      fixtures['/api/reports'] = original
    }
    fixtures['/api/reports'] = [{ ...base, justification: { conflicting_evidence: { supported: true, statement: 's', evidence_ids: ['e1'], basis: 'nli_verified' } } }]
    try {
      cleanup()
      render(<App />)
      expect(await screen.findByText('Conflict verified by NLI')).toBeInTheDocument()
    } finally {
      fixtures['/api/reports'] = original
    }
  })

  it('shows a sentence stored under two labels once, with both labels and its citations', async () => {
    const original = fixtures['/api/clusters']
    fixtures['/api/clusters'] = [{
      id: 4, label: 'Multilingual robustness', member_count: 1, contradiction_flag: false, contradiction_score: 0,
      members: [{ point_id: 'p1', point_ids: ['p1', 'p2'], paper_id: 1, title: 'A paper', text: 'Prior detectors [12] fail across languages.', year: 2024, source_type: 'future_work', source_types: ['future_work', 'limitation'], citation_markers: ['[12]'] }],
    }]
    try {
      const user = userEvent.setup()
      render(<App />)
      await screen.findByText('Multilingual prompt injection remains underexplored.')
      await user.click(screen.getByRole('button', { name: /^clusters$/i }))
      expect(await screen.findByText('1 statement')).toBeInTheDocument()
      expect(screen.getAllByText('Prior detectors [12] fail across languages.')).toHaveLength(1)
      expect(screen.getByText(/future work, limitation/)).toBeInTheDocument()
      expect(screen.getByText(/cites \[12\]/)).toBeInTheDocument()
    } finally {
      fixtures['/api/clusters'] = original
    }
  })

  it('labels a single-paper gap exploratory and shows each claim with its basis', async () => {
    const saved = { gaps: fixtures['/api/gaps?limit=100'], reports: fixtures['/api/reports'] }
    const gap = (saved.gaps as Record<string, unknown>[])[0]
    const report = (saved.reports as Record<string, unknown>[])[0]
    fixtures['/api/gaps?limit=100'] = [{ ...gap, paper_count: 1, evidence_scope: 'single_paper', exploratory: true }]
    fixtures['/api/reports'] = [{
      ...report,
      justification: {
        problem_evidence: { supported: true, statement: 'Attacks succeed.', evidence_ids: ['e1'], basis: 'llm_cited' },
        solution_insufficiency_evidence: { supported: false, statement: 'Not shown.', evidence_ids: [], basis: 'not_supported' },
        research_gap_evidence: { supported: true, statement: 'Bangla is untested.', evidence_ids: ['e1'], basis: 'llm_cited' },
        conflicting_evidence: { supported: false, statement: 'They disagree.', evidence_ids: ['e1'], basis: 'llm_unverified', llm_supported: true },
      },
    }]
    try {
      render(<App />)
      expect(await screen.findByText('Exploratory · single paper')).toBeInTheDocument()
      expect(screen.getByText(/stated by one paper only/)).toBeInTheDocument()
      expect(screen.getAllByText('Model judgment, cites retrieved passages')).toHaveLength(2)
      expect(screen.getByText('Not supported by the retrieved passages')).toBeInTheDocument()
      expect(screen.getByText('Asserted by the model, not confirmed by NLI')).toBeInTheDocument()
      expect(screen.queryByText('Conflict verified by NLI')).not.toBeInTheDocument()
    } finally {
      fixtures['/api/gaps?limit=100'] = saved.gaps
      fixtures['/api/reports'] = saved.reports
    }
  })

  it('qualifies trends computed from a handful of papers and a partial year', async () => {
    const saved = fixtures['/api/trends']
    fixtures['/api/trends'] = [{ cluster_id: 4, yearly_counts: { 2024: 1, 2025: 4, 2026: 4 }, slope: 1.5, qualifiers: { papers: 9, years: 3, max_papers_per_year: 4, small_sample: true, partial_years: ['2026'], single_year: false } }]
    try {
      const user = userEvent.setup()
      render(<App />)
      await screen.findByText('Multilingual prompt injection remains underexplored.')
      await user.click(screen.getByRole('button', { name: /^trends$/i }))
      expect(await screen.findByText(/at most 4 papers in any year/)).toBeInTheDocument()
      expect(screen.getByText(/2026 is a partial year/)).toBeInTheDocument()
      expect(screen.getByText('9 papers')).toBeInTheDocument()
      expect(screen.queryByText(/\b1 papers\b/)).not.toBeInTheDocument()
    } finally {
      fixtures['/api/trends'] = saved
    }
  })

  it('says "1 paper", never "1 papers", when a trend rests on a single paper', async () => {
    const saved = fixtures['/api/trends']
    fixtures['/api/trends'] = [{ cluster_id: 4, yearly_counts: { 2025: 1 }, slope: 0, qualifiers: { papers: 1, years: 1, max_papers_per_year: 1, small_sample: true, partial_years: [], single_year: true } }]
    try {
      const user = userEvent.setup()
      render(<App />)
      await screen.findByText('Multilingual prompt injection remains underexplored.')
      await user.click(screen.getByRole('button', { name: /^trends$/i }))
      expect(await screen.findByText(/^1 paper · one year, no trend$/)).toBeInTheDocument()
      expect(screen.getByText(/at most 1 paper in any year/)).toBeInTheDocument()
      // The guard above can only fail if a "1 papers" is rendered somewhere.
      expect(screen.queryByText(/\b1 papers\b/)).not.toBeInTheDocument()
    } finally {
      fixtures['/api/trends'] = saved
    }
  })

  it('does not present one corpus\'s trends as the field moving', async () => {
    const saved = fixtures['/api/trends']
    fixtures['/api/trends'] = [{ cluster_id: 4, yearly_counts: { 2024: 2, 2025: 1 }, slope: -1, qualifiers: { papers: 3, years: 2, max_papers_per_year: 2, small_sample: true, partial_years: [], single_year: false } }]
    try {
      const user = userEvent.setup()
      render(<App />)
      await screen.findByText('Multilingual prompt injection remains underexplored.')
      await user.click(screen.getByRole('button', { name: /^trends$/i }))
      expect(await screen.findByText(/describe this corpus, not the field/)).toBeInTheDocument()
      expect(screen.queryByText(/where the field is moving/i)).not.toBeInTheDocument()
      // A falling slope is listed too, so the list is not "fastest rising".
      expect(screen.queryByText(/fastest rising/i)).not.toBeInTheDocument()
      expect(screen.getByText('-1.00')).toBeInTheDocument()
    } finally {
      fixtures['/api/trends'] = saved
    }
  })

  it('reads a conflict claim with no recorded basis as unconfirmed, never as cited support', async () => {
    // A report stored before D-09-13 (or an archived snapshot of one) has no
    // `basis`; its conflict claim was never NLI-checked.
    const saved = fixtures['/api/reports']
    const report = (saved as Record<string, unknown>[])[0]
    fixtures['/api/reports'] = [{
      ...report,
      justification: {
        problem_evidence: { supported: true, statement: 'Attacks succeed.', evidence_ids: ['e1'] },
        conflicting_evidence: { supported: true, statement: 'Studies disagree.', evidence_ids: ['e1'] },
      },
    }]
    try {
      render(<App />)
      expect(await screen.findByText('Asserted by the model, not confirmed by NLI')).toBeInTheDocument()
      expect(screen.getAllByText('Model judgment, cites retrieved passages')).toHaveLength(1)
      expect(screen.queryByText('Conflict verified by NLI')).not.toBeInTheDocument()
    } finally {
      fixtures['/api/reports'] = saved
    }
  })

  it('marks a quote that stops at the stored length cap', async () => {
    const saved = fixtures['/api/reports']
    const report = (saved as Record<string, unknown>[])[0]
    fixtures['/api/reports'] = [{
      ...report,
      evidence: [
        { id: 'e1', paper_id: 1, title: 'A paper', text: 'Methods degrade outside English', truncated: true },
        { id: 'e2', paper_id: 1, title: 'A paper', text: 'A short, complete passage.', truncated: false },
      ],
    }]
    try {
      render(<App />)
      expect(await screen.findByText('Methods degrade outside English…')).toBeInTheDocument()
      expect(screen.getAllByText(/passage may continue/)).toHaveLength(1)
      expect(screen.getByText('A short, complete passage.')).toBeInTheDocument()
    } finally {
      fixtures['/api/reports'] = saved
    }
  })

  it('does not count archived reports as current ranked reports', async () => {
    const saved = fixtures['/api/reports']
    fixtures['/api/reports'] = [{ ...(saved as object[])[0], stale: true, snapshot_id: 'old-run' }]
    try {
      render(<App />)
      await screen.findByText('Previous report — refresh pending')
      expect(screen.getByText('Ranked reports').parentElement?.querySelector('strong')?.textContent).toBe('0')
    } finally {
      fixtures['/api/reports'] = saved
    }
  })

  it('explores a cluster and reveals the differently-phrased statements it groups', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^clusters$/i }))

    expect(await screen.findByText('Multilingual robustness')).toBeInTheDocument()
    expect(screen.getByText('7 statements')).toBeInTheDocument()
    expect(screen.getByText('Performance decreases for multilingual datasets.')).toBeInTheDocument()
    expect(screen.getByText('Existing datasets focus primarily on English.')).toBeInTheDocument()
  })

  it('keeps every answered question in the Mode B transcript', async () => {
    const user = userEvent.setup()
    const fallbackFetch = vi.mocked(fetch)
    let answered = 0
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/query')) {
        answered += 1
        // A grounded answer is its checked claims (`answer_from_claims`).
        const claim = `Answer number ${answered}.`
        return Promise.resolve(new Response(
          JSON.stringify({ ...queryPayload(), answer: claim, claims: [{ text: claim, evidence_ids: ['e1'] }] }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ))
      }
      return fallbackFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^ask scholarlens$/i }))

    await user.type(screen.getByLabelText(/research question/i), 'First question?')
    await user.click(screen.getByRole('button', { name: /analyze evidence/i }))
    expect(await screen.findByText('Answer number 1.')).toBeInTheDocument()

    await user.type(screen.getByLabelText(/research question/i), 'Second question?')
    await user.click(screen.getByRole('button', { name: /analyze evidence/i }))
    expect(await screen.findByText('Answer number 2.')).toBeInTheDocument()

    // The earlier exchange stays readable rather than being overwritten.
    expect(screen.getByText('Answer number 1.')).toBeInTheDocument()
    expect(screen.getByText('First question?')).toBeInTheDocument()
    expect(screen.getByText('Second question?')).toBeInTheDocument()
  })

  it.each([
    ['grounding_failed', 'A grounded answer could not be produced from the retrieved evidence.', 'Answer not verified'],
    ['insufficient_evidence', 'Insufficient relevant evidence was found after the allowed retrieval rounds.', 'Evidence incomplete'],
  ])('never lists sources beside a %s refusal', async (outcome, answer, label) => {
    const user = userEvent.setup()
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/query')) {
        return new Response(JSON.stringify({
          outcome,
          answer,
          claims: [],
          // An insufficient-evidence refusal still returns what was retrieved.
          citations: outcome === 'insufficient_evidence' ? [{ id: 'e9', paper_id: 3, title: 'Unrelated retrieved paper', text: 'A retrieved passage.', year: 2025, source_type: 'paragraph' }] : [],
          grounded: false,
          retry_count: 0,
          trace: [],
          provider: 'groq',
          model: 'openai/gpt-oss-20b',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return populatedFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^ask scholarlens$/i }))
    await user.type(screen.getByLabelText(/research question/i), 'Which languages were tested?')
    await user.click(screen.getByRole('button', { name: /analyze evidence/i }))

    expect(await screen.findByText(label)).toBeInTheDocument()
    expect(screen.queryByText('No indexed evidence')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Sources' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Cited passages' })).not.toBeInTheDocument()
    expect(screen.queryByText(/checked automatically/i)).not.toBeInTheDocument()
    expect(screen.queryByText('Unrelated retrieved paper')).not.toBeInTheDocument()
  })

  it('attributes a Mode B citation without offering a link the API cannot provide', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^ask scholarlens$/i }))
    await user.type(screen.getByLabelText(/research question/i), 'What gaps exist?')
    await user.click(screen.getByRole('button', { name: /analyze evidence/i }))
    await screen.findByText('Low-resource languages remain under-supported.')

    const citation = screen.getByText('Methods degrade outside English.').closest('.citation')
    expect(citation).not.toBeNull()
    expect(citation?.textContent).toContain('2024')
    // The backend exposes no URL for an evidence chunk, so nothing may look clickable.
    expect(citation?.querySelector('a')).toBeNull()
  })

  it('stays usable when an endpoint returns an unexpected payload shape', async () => {
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/reports')) {
        return Promise.resolve(new Response(JSON.stringify({ detail: 'unexpected object' }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }))
      }
      return populatedFetch(input, init)
    }))
    render(<App />)

    // The ranking still renders from /gaps instead of the whole dashboard collapsing.
    expect(await screen.findByText('Cluster 4: explanation pending')).toBeInTheDocument()
    expect(screen.queryByText('Connection needed')).not.toBeInTheDocument()
  })

  it('lists every paper the collection returned, not a truncated preview', async () => {
    const user = userEvent.setup()
    const collected = Array.from({ length: 12 }, (_, index) => ({
      id: index + 2,
      title: `Collected paper ${index + 1}`,
      authors: ['A'],
      year: 2025,
      venue: 'ACL',
      source: 'arxiv',
      has_full_text: true,
      citation_count: 0,
    }))
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/papers')) {
        return new Response(JSON.stringify([...(fixtures['/api/papers?limit=500'] as object[]), ...collected]), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.endsWith('/collect')) {
        return new Response(JSON.stringify({
          count: 12,
          ids: collected.map((paper) => paper.id),
          added: 10,
          duplicates: { total: 2, cross_source: 2, within_source: 0, already_in_corpus: 0 },
          sources: { arxiv: 'ok', semantic_scholar: 'ok', acl_anthology: 'ok', unpaywall: 'ok' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return populatedFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    await user.type(screen.getByLabelText(/research topic/i), 'multilingual prompt injection')
    await user.click(screen.getByRole('button', { name: /find papers/i }))

    // All twelve, not the first six.
    expect(await screen.findByText('Collected paper 12')).toBeInTheDocument()
    for (let index = 1; index <= 12; index += 1) {
      expect(screen.getByText(`Collected paper ${index}`)).toBeInTheDocument()
    }
    // A paper the corpus already held but this search did not return is not
    // presented as part of it.
    expect(screen.queryByText('A paper')).not.toBeInTheDocument()
  })

  it('reports duplicates alongside the papers a collection added', async () => {
    const user = userEvent.setup()
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/collect')) {
        return new Response(JSON.stringify({
          count: 22,
          ids: [1],
          added: 22,
          duplicates: { total: 8, cross_source: 5, within_source: 1, already_in_corpus: 2 },
          sources: { arxiv: 'ok', semantic_scholar: 'ok', acl_anthology: 'ok', unpaywall: 'ok' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return populatedFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    await user.type(screen.getByLabelText(/research topic/i), 'prompt injection')
    await user.click(screen.getByRole('button', { name: /find papers/i }))

    expect(await screen.findByText(/22 papers added/)).toBeInTheDocument()
    expect(screen.getByText(/8 duplicates/)).toBeInTheDocument()
    expect(screen.getByText(/5 across sources/)).toBeInTheDocument()
    expect(screen.getByText(/2 already in the corpus/)).toBeInTheDocument()
  })

  it('shows the run status, its warnings and full-text coverage', async () => {
    const user = userEvent.setup()
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/collect')) {
        return new Response(JSON.stringify({
          count: 20,
          ids: [1],
          added: 20,
          sources: { arxiv: 'ok', semantic_scholar: 'ok', acl_anthology: 'ok', fulltext: 'ok' },
          status: 'completed_with_warnings',
          warnings: [
            { source: 'semantic_scholar', code: 'retrieval_ceiling', message: 'semantic_scholar: read 100 of 16451 reported results (ceiling).' },
            { source: 'acl_anthology', code: 'stale_cache', message: 'ACL Anthology export is 21 days old (limit 7); refresh failed.' },
          ],
          full_text: {
            touched: { papers: 20, with_full_text: 12, metadata_only: 8, metadata_only_without_doi: 1, coverage: 0.6 },
            added: { papers: 20, with_full_text: 12, metadata_only: 8, metadata_only_without_doi: 1, coverage: 0.6 },
          },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return populatedFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    await user.type(screen.getByLabelText(/research topic/i), 'prompt injection')
    await user.click(screen.getByRole('button', { name: /find papers/i }))

    expect(await screen.findByText(/20 papers added/)).toBeInTheDocument()
    expect(screen.getByText('Completed with warnings')).toBeInTheDocument()
    expect(screen.getByText(/read 100 of 16451 reported results/)).toBeInTheDocument()
    expect(screen.getByText(/21 days old/)).toBeInTheDocument()
    expect(screen.getByText('Full text for 12 of 20 papers; 8 metadata only (1 without a DOI)')).toBeInTheDocument()
  })

  it('shows no run warnings after a clean collection', async () => {
    const user = userEvent.setup()
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/collect')) {
        return new Response(JSON.stringify({
          count: 1, ids: [1], added: 1, status: 'completed', warnings: [],
          sources: { arxiv: 'ok', semantic_scholar: 'ok', acl_anthology: 'ok', fulltext: 'ok' },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return populatedFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    await user.type(screen.getByLabelText(/research topic/i), 'prompt injection')
    await user.click(screen.getByRole('button', { name: /find papers/i }))

    expect(await screen.findByText(/1 paper added/)).toBeInTheDocument()
    expect(screen.queryByText(/Completed with warnings/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Partly completed/)).not.toBeInTheDocument()
  })

  it('leaves the corpus untouched when the delete confirmation is declined', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))

    await user.click(screen.getByRole('button', { name: /delete corpus/i }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(/this will remove all the stored papers/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^no$/i }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false)
    expect(screen.getByText('A paper')).toBeInTheDocument()
  })

  it('deletes the corpus and empties the library once the dialog is confirmed', async () => {
    const user = userEvent.setup()
    let deleted = false
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/admin/corpus') && init?.method === 'DELETE') {
        deleted = true
        return new Response(JSON.stringify({ deleted: true, papers: 1, pdfs: 0 }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/papers')) {
        return new Response(JSON.stringify(deleted ? [] : fixtures['/api/papers?limit=500']), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        })
      }
      return populatedFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))
    expect(screen.getByText('A paper')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /delete corpus/i }))
    await user.click(await screen.findByRole('button', { name: /^yes$/i }))

    expect(await screen.findByText('Corpus deleted successfully.')).toBeInTheDocument()
    const request = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === 'DELETE')
    expect(String(request?.[0])).toContain('/admin/corpus')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('A paper')).not.toBeInTheDocument()
    expect(await screen.findByText('Your corpus is empty')).toBeInTheDocument()
  })

  it('keeps the corpus visible and reports why a failed deletion did not happen', async () => {
    const user = userEvent.setup()
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/admin/corpus') && init?.method === 'DELETE') {
        return new Response(JSON.stringify({ detail: 'Corpus reset failed' }), {
          status: 502, headers: { 'Content-Type': 'application/json' },
        })
      }
      return populatedFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^add papers$/i }))

    await user.click(screen.getByRole('button', { name: /delete corpus/i }))
    await user.click(await screen.findByRole('button', { name: /^yes$/i }))

    expect(await screen.findByText('Corpus reset failed')).toBeInTheDocument()
    // The corpus is still there, and the button is usable again.
    expect(screen.getByText('A paper')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /delete corpus/i })).toBeEnabled()
  })

  it('closes the API connection panel with the Escape key', async () => {
    const user = userEvent.setup()
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')

    await user.click(screen.getByRole('button', { name: /connect api/i }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByLabelText(/api key/i)).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  describe('API key handling', () => {
    beforeEach(() => sessionStorage.clear())
    afterEach(() => sessionStorage.clear())

    function keyCheckingFetch() {
      const populatedFetch = vi.mocked(fetch)
      vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (new Headers(init?.headers).get('X-API-Key') !== 'valid-dummy-key') {
          return new Response(JSON.stringify({ detail: 'Invalid or missing API key' }), { status: 401 })
        }
        return populatedFetch(input, init)
      }))
    }

    it('asks for a key, rather than calling it rejected, when none has been entered', async () => {
      keyCheckingFetch()
      render(<App />)

      expect(await screen.findByText('Connection needed')).toBeInTheDocument()
      expect(screen.getByText(/enter the api key/i)).toBeInTheDocument()
      expect(screen.queryByText(/was rejected/i)).not.toBeInTheDocument()
    })

    it('recovers in place once a rejected key is replaced with a valid one', async () => {
      const user = userEvent.setup()
      sessionStorage.setItem('scholarlens-api-key', 'wrong-dummy-key')
      keyCheckingFetch()
      render(<App />)
      expect(await screen.findByText(/was rejected/i)).toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: /open api settings/i }))
      await user.clear(screen.getByLabelText(/api key/i))
      await user.type(screen.getByLabelText(/api key/i), 'valid-dummy-key')
      await user.click(screen.getByRole('button', { name: /save and reconnect/i }))

      expect(await screen.findByText('Multilingual prompt injection remains underexplored.')).toBeInTheDocument()
      expect(screen.queryByText('Connection needed')).not.toBeInTheDocument()
      expect(screen.getByText('Evidence pipeline connected')).toBeInTheDocument()
    })
  })

  async function askWith(response: () => Response, question = 'Which languages were tested?') {
    const user = userEvent.setup()
    const populatedFetch = vi.mocked(fetch)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => (String(input).endsWith('/query') ? response() : populatedFetch(input, init)))
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^ask scholarlens$/i }))
    await user.type(screen.getByLabelText(/research question/i), question)
    await user.click(screen.getByRole('button', { name: /analyze evidence/i }))
    return { user, fetchMock }
  }

  it('ties each claim of a grounded answer to the numbered passages that support it', async () => {
    await askWith(() => new Response(JSON.stringify({
      ...queryPayload(),
      outcome: 'grounded_answer',
      answer: 'Claim one. Claim two.',
      claims: [
        { text: 'Claim one.', evidence_ids: ['e1'] },
        // An id the citations do not carry gets no number: never invent a source.
        { text: 'Claim two.', evidence_ids: ['e2', 'e1', 'unknown'] },
      ],
      citations: [
        { id: 'e1', paper_id: 1, title: 'A paper', text: 'Passage one.', year: 2024 },
        { id: 'e2', paper_id: 2, title: 'Another paper', text: 'Passage two.', year: 2025 },
      ],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    const first = (await screen.findByText('Claim one.')).closest('.answer-claim')
    const second = screen.getByText('Claim two.').closest('.answer-claim')
    expect(first?.textContent).toBe('Claim one.[1]')
    expect(second?.textContent).toBe('Claim two.[1][2]')
    expect(screen.getByText('Passage one.').closest('.citation')?.textContent).toMatch(/^1/)
    expect(screen.getByText('Passage two.').closest('.citation')?.textContent).toMatch(/^2/)
  })

  it('renders a real live grounded answer with its claim tied to the cited passage', async () => {
    // Recorded live output (LV-1b), not a hand-written mock: the claim, its
    // evidence id and the passage text are exactly what /query returned.
    await askWith(() => new Response(JSON.stringify(liveGroundedAnswer), { status: 200, headers: { 'Content-Type': 'application/json' } }),
      'Which natural language had the highest full compliance rate for malicious prompts?')

    const claim = (await screen.findByText('Turkish had the highest full compliance rate.')).closest('.answer-claim')
    expect(claim?.textContent).toBe('Turkish had the highest full compliance rate.[1]')
    expect(screen.getByLabelText('cited passage 1')).toBeInTheDocument()
    const citation = screen.getByText(/Turkish shows the highest full compliance \(80\.18%\)/).closest('.citation')
    expect(citation?.textContent).toMatch(/^1/)
    expect(citation).toHaveTextContent(liveGroundedAnswer.citations[0].title)
    expect(screen.getByText('Grounded answer')).toBeInTheDocument()
    expect(screen.getByText(/not verified by a person/)).toBeInTheDocument()
    expect(screen.getAllByText(/^Turkish had the highest/)).toHaveLength(1)
  })

  it('counts retrieval retries in plain English', async () => {
    await askWith(() => new Response(JSON.stringify({ ...queryPayload(), retry_count: 2 }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    expect(await screen.findByText(/2 retrieval retries/)).toBeInTheDocument()
  })

  it('says when to try again after a rate-limited question, without retrying by itself', async () => {
    const { fetchMock } = await askWith(() => new Response(JSON.stringify({ detail: 'Groq rate limit reached.' }), { status: 429, headers: { 'Content-Type': 'application/json', 'Retry-After': '30' } }))
    expect(await screen.findByText(/Groq rate limit reached\. Try again in about 30 seconds\./)).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/query'))).toHaveLength(1)
    // The question stays in the box so it can be resubmitted by hand.
    expect(screen.getByLabelText(/research question/i)).toHaveValue('Which languages were tested?')
  })

  it('explains a rejected key on a question the same way as on the dashboard', async () => {
    await askWith(() => new Response(JSON.stringify({ detail: 'Invalid or missing API key' }), { status: 401 }))
    expect(await screen.findByText(/was rejected/i)).toBeInTheDocument()
  })

  it('shows the statements behind a cluster\'s conflict flag', async () => {
    const user = userEvent.setup()
    const populatedFetch = vi.mocked(fetch)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/api/clusters')) {
        return new Response(JSON.stringify([{
          ...(fixtures['/api/clusters'] as object[])[0],
          contradictions: [{ left_point_id: 'p1', left_paper_id: 1, left_text: 'Accuracy rises with model size.', right_point_id: 'p9', right_paper_id: 2, right_text: 'Accuracy falls with model size.', score: 0.93 }],
        }]), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return populatedFetch(input, init)
    }))
    render(<App />)
    await screen.findByText('Multilingual prompt injection remains underexplored.')
    await user.click(screen.getByRole('button', { name: /^clusters$/i }))

    expect(await screen.findByText('Conflicting statements')).toBeInTheDocument()
    const left = screen.getByText('Accuracy rises with model size.').closest('blockquote')
    const right = screen.getByText('Accuracy falls with model size.').closest('blockquote')
    expect(left).toHaveTextContent('A paper')
    expect(right).toHaveTextContent('Paper 2')
  })

  describe('Mode B quality (2026-10-18)', () => {
    function answerWith(payload: Record<string, unknown>, status = 200) {
      const populatedFetch = vi.mocked(fetch)
      vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith('/query')) {
          return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } })
        }
        return populatedFetch(input, init)
      }))
    }

    async function ask(question = 'Which papers report weaker safety outside English?') {
      const user = userEvent.setup()
      render(<App />)
      await screen.findByText('Multilingual prompt injection remains underexplored.')
      await user.click(screen.getByRole('button', { name: /^ask scholarlens$/i }))
      await user.type(screen.getByLabelText(/research question/i), question)
      await user.click(screen.getByRole('button', { name: /analyze evidence/i }))
      return user
    }

    it('says how many unsupported statements were withheld from a partial answer', async () => {
      answerWith({ ...queryPayload(), outcome: 'grounded_answer', removed_claim_count: 2 })
      await ask()
      expect(await screen.findByText('Low-resource languages remain under-supported.')).toBeInTheDocument()
      expect(screen.getByText(/2 statements from the draft were withheld because their cited passages did not support them/i)).toBeInTheDocument()
      expect(screen.getByText(/may be incomplete/i)).toBeInTheDocument()
    })

    it.each([
      ['a complete answer', { ...queryPayload(), outcome: 'grounded_answer', removed_claim_count: 0 }],
      ['an older API', queryPayload()],
      ['a refusal', { outcome: 'grounding_failed', answer: 'A grounded answer could not be produced from the retrieved evidence.', claims: [], citations: [], grounded: false, retry_count: 0, trace: [], provider: 'groq', model: 'm', removed_claim_count: 0 }],
    ])('shows no withheld note for %s', async (_name, payload) => {
      answerWith(payload)
      await ask()
      await screen.findByText(/Low-resource languages remain under-supported\.|A grounded answer could not be produced/)
      expect(screen.queryByText(/withheld/i)).not.toBeInTheDocument()
    })

    it.each([
      [504, 'The question was not answered within the 180-second limit. No answer was produced; try a narrower question or retry later.'],
      [502, 'Query generation failed'],
      [503, 'Groq is unavailable. Check network access.'],
    ])('presents a %s as a service problem, never as an evidence finding', async (status, detail) => {
      answerWith({ detail }, status)
      await ask()
      const banner = await screen.findByText((text) => text.startsWith(detail))
      expect(banner.textContent).toContain('This is a service problem, not a finding about the literature.')
      expect(screen.queryByText('Evidence incomplete')).not.toBeInTheDocument()
      expect(screen.queryByText('Answer not verified')).not.toBeInTheDocument()
      expect(document.querySelector('.answer-card')).toBeNull()
    })

    it('never shows the passages of an earlier answer under a new question', async () => {
      const populatedFetch = vi.mocked(fetch)
      const answers = [
        { ...queryPayload(), answer: 'First claim.', claims: [{ text: 'First claim.', evidence_ids: ['a'] }], citations: [{ id: 'a', paper_id: 1, title: 'Paper A', text: 'Passage A.' }] },
        { ...queryPayload(), answer: 'Second claim.', claims: [{ text: 'Second claim.', evidence_ids: ['b'] }], citations: [{ id: 'b', paper_id: 2, title: 'Paper B', text: 'Passage B.' }] },
      ]
      vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith('/query')) {
          return new Response(JSON.stringify(answers.shift()), { status: 200, headers: { 'Content-Type': 'application/json' } })
        }
        return populatedFetch(input, init)
      }))
      const user = await ask('First question?')
      await screen.findByText('First claim.')
      await user.type(screen.getByLabelText(/research question/i), 'Second question?')
      await user.click(screen.getByRole('button', { name: /analyze evidence/i }))
      await screen.findByText('Second claim.')
      const cards = document.querySelectorAll('.answer-card')
      expect(cards).toHaveLength(2)
      expect(cards[1].textContent).toContain('Passage B.')
      expect(cards[1].textContent).not.toContain('Passage A.')
      expect(cards[0].textContent).not.toContain('Passage B.')
    })
  })
})
