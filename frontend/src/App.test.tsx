import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

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
        return Promise.resolve(new Response(
          JSON.stringify({ ...queryPayload(), answer: `Answer number ${answered}.` }),
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
})
