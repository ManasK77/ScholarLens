import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Cluster, Trend } from '../types'

// Plotly needs a real canvas; capture what the chart would be drawn from instead.
const drawn: Array<{ data: Array<{ hovertemplate: string }>; layout: { yaxis: { title: { text: string } } } }> = []
vi.mock('plotly.js-basic-dist-min', () => ({ default: {} }))
vi.mock('react-plotly.js/factory', () => ({
  default: () => (props: (typeof drawn)[number]) => {
    drawn.push(props)
    return null
  },
}))

const { default: TrendChart } = await import('./TrendChart')

describe('TrendChart', () => {
  afterEach(() => {
    cleanup()
    drawn.length = 0
  })

  it('labels the series as distinct papers, which is what it counts', () => {
    const trends: Trend[] = [{ cluster_id: 4, yearly_counts: { 2024: 1, 2025: 4 }, slope: 3 }]
    const clusters: Cluster[] = [{ id: 4, label: 'Multilingual robustness', member_count: 7, contradiction_flag: false, contradiction_score: 0, members: [] }]
    render(<TrendChart trends={trends} clusters={clusters} />)
    const [{ data, layout }] = drawn
    expect(layout.yaxis.title.text).toBe('Papers per year')
    expect(data[0].hovertemplate).toContain('papers in %{x}')
    expect(JSON.stringify({ data, layout })).not.toMatch(/mention/i)
  })
})
