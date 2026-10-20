import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, ScholarLensApi } from './api'

// Dummy credential: these tests never reach a real backend.
const KEY = 'dummy-test-key'

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function stubFetch(respond: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => respond(String(input), init))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError)
    return error as ApiError
  }
  throw new Error('expected the request to fail')
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ScholarLensApi request construction', () => {
  it('defaults to the same-origin /api proxy and sends the key as X-API-Key', async () => {
    const fetchMock = stubFetch(() => json({ outcome: 'no_indexed_evidence' }))

    await new ScholarLensApi(KEY).query('What gaps exist?')

    const [url, init] = fetchMock.mock.calls[0]
    const headers = new Headers(init?.headers)
    expect(url).toBe('/api/query')
    expect(headers.get('X-API-Key')).toBe(KEY)
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({ question: 'What gaps exist?' })
  })

  it('uses an explicitly configured base URL', async () => {
    const fetchMock = stubFetch(() => json({ created: [1] }))

    await new ScholarLensApi(KEY, 'https://api.example.test').upload([new File(['%PDF'], 'a.pdf')])

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('https://api.example.test/upload')
    // The browser sets the multipart boundary; a JSON content type would break it.
    expect(new Headers(init?.headers).get('Content-Type')).toBeNull()
    expect(init?.body).toBeInstanceOf(FormData)
  })

  it('never sends an empty key header and never puts the key in the URL', async () => {
    const fetchMock = stubFetch(() => json([]))

    await new ScholarLensApi('').dashboard()

    for (const [url, init] of fetchMock.mock.calls) {
      expect(new Headers(init?.headers).has('X-API-Key')).toBe(false)
      expect(url).not.toContain(KEY)
    }
  })
})

describe('ScholarLensApi error handling', () => {
  it('surfaces a rejected key as a 401 ApiError carrying the server detail', async () => {
    stubFetch(() => json({ detail: 'Invalid or missing API key' }, 401))

    const error = await failure(new ScholarLensApi('wrong').query('What gaps exist?'))

    expect(error.status).toBe(401)
    expect(error.message).toBe('Invalid or missing API key')
  })

  it('turns a FastAPI validation error list into a readable message', async () => {
    stubFetch(() => json({
      detail: [{ loc: ['body', 'topic'], msg: 'String should have at most 500 characters', type: 'string_too_long' }],
    }, 422))

    const error = await failure(new ScholarLensApi(KEY).collect('x'.repeat(501)))

    expect(error.status).toBe(422)
    expect(error.message).toBe('Invalid request: String should have at most 500 characters')
  })

  it('reports an unreachable API as a network failure, not a raw TypeError', async () => {
    stubFetch(() => { throw new TypeError('Failed to fetch') })

    const error = await failure(new ScholarLensApi(KEY).query('What gaps exist?'))

    expect(error.status).toBe(0)
    expect(error.message).toBe('Could not reach the ScholarLens API.')
  })

  it('keeps an aborted request distinguishable from a network failure', async () => {
    stubFetch(() => { throw new DOMException('The operation was aborted.', 'AbortError') })

    await expect(new ScholarLensApi(KEY).runPipeline(true)).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('falls back to the status for a non-JSON server error', async () => {
    stubFetch(() => new Response('<html>Bad Gateway</html>', { status: 502 }))

    const error = await failure(new ScholarLensApi(KEY).query('What gaps exist?'))

    expect(error.status).toBe(502)
    expect(error.message).toBe('Request failed (502)')
  })

  it('passes the rate-limit detail through on a 429', async () => {
    stubFetch(() => json({ detail: 'Groq rate limit reached.' }, 429))

    const error = await failure(new ScholarLensApi(KEY).query('What gaps exist?'))

    expect(error.status).toBe(429)
    expect(error.message).toBe('Groq rate limit reached.')
    expect(error.retryAfter).toBeUndefined()
  })

  it('carries the Retry-After wait of a 429 so the UI can say when to try again', async () => {
    stubFetch(() => new Response(JSON.stringify({ detail: 'Groq rate limit reached.' }), { status: 429, headers: { 'Content-Type': 'application/json', 'Retry-After': '30' } }))

    const error = await failure(new ScholarLensApi(KEY).query('What gaps exist?'))

    expect(error.retryAfter).toBe(30)
  })

  it('rejects a successful status whose body is not JSON instead of throwing a parser error', async () => {
    stubFetch(() => new Response('<!doctype html><title>proxy</title>', { status: 200 }))

    const error = await failure(new ScholarLensApi(KEY).query('What gaps exist?'))

    expect(error.message).toBe('The API returned a response that could not be read.')
  })
})

describe('ScholarLensApi response interpretation', () => {
  it('returns a Mode B refusal unchanged, with no claims and no citations', async () => {
    const refusal = {
      outcome: 'insufficient_evidence',
      answer: '',
      claims: [],
      citations: [],
      grounded: false,
      retrieval_rounds: 3,
      retry_count: 2,
      regeneration_count: 0,
      trace: ['reason=thin'],
      provider: 'groq',
      model: 'm',
    }
    stubFetch(() => json(refusal))

    expect(await new ScholarLensApi(KEY).query('What gaps exist?')).toEqual(refusal)
  })

  it('still rejects the whole dashboard on an auth failure from an optional panel', async () => {
    stubFetch((url) => (url.includes('/graph') ? json({ detail: 'Invalid or missing API key' }, 401) : json([])))

    const error = await failure(new ScholarLensApi(KEY).dashboard())

    expect(error.status).toBe(401)
  })

  it('keeps the dashboard usable when only the graph service is down', async () => {
    stubFetch((url) => (url.includes('/graph') ? json({ detail: 'Internal server error' }, 500) : json([])))

    const data = await new ScholarLensApi(KEY).dashboard()

    expect(data.graph).toEqual({ nodes: [], edges: [], total_nodes: undefined, total_edges: undefined })
    expect(data.papers).toEqual([])
  })
})
