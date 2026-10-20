import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import KnowledgeGraph from './KnowledgeGraph'
import type { GraphSnapshot } from '../types'

const nodes = Array.from({ length: 200 }, (_, index) => ({
  id: `n${index}`,
  labels: [index < 20 ? 'Paper' : 'Entity'],
  label: `Node ${index}`,
}))

describe('KnowledgeGraph', () => {
  afterEach(cleanup)

  it('says how much of a larger graph it is showing', () => {
    const graph: GraphSnapshot = { nodes, edges: [], total_nodes: 632, total_edges: 744 }
    render(<KnowledgeGraph graph={graph} />)
    expect(screen.getByText('Showing 180 of 632 nodes, papers first, then the most connected entities.')).toBeInTheDocument()
  })

  it('keeps the truncation note clear of the legend overlay', () => {
    const graph: GraphSnapshot = { nodes, edges: [], total_nodes: 632, total_edges: 744 }
    render(<KnowledgeGraph graph={graph} />)
    // The legend is absolutely positioned inside the canvas frame; the note
    // must sit outside that frame or the legend covers it.
    expect(screen.getByText(/^Showing 180 of 632/).closest('.graph-wrap')).toBeNull()
  })

  it('lists a selected node\'s relationships so the graph can be explored', async () => {
    const user = userEvent.setup()
    const graph: GraphSnapshot = {
      nodes: [
        { id: 'p1', labels: ['Paper'], label: 'Paper A' },
        { id: 'e1', labels: ['Entity'], label: 'BERT' },
        { id: 'e2', labels: ['Entity'], label: 'SQuAD' },
      ],
      edges: [
        { source: 'p1', target: 'e1', type: 'MENTIONS' },
        { source: 'e1', target: 'e2', type: 'EVALUATED_USING' },
      ],
    }
    render(<KnowledgeGraph graph={graph} />)

    await user.click(await screen.findByRole('button', { name: 'BERT' }))

    const panel = screen.getByRole('status')
    expect(panel).toHaveTextContent('BERT')
    expect(panel).toHaveTextContent('2 connections')
    expect(panel).toHaveTextContent('Paper A')
    expect(panel).toHaveTextContent('mentions')
    expect(panel).toHaveTextContent('SQuAD')
    expect(panel).toHaveTextContent('evaluated using')
  })

  it('selects a node with the Space key, like any button', async () => {
    const user = userEvent.setup()
    const graph: GraphSnapshot = { nodes: [{ id: 'p1', labels: ['Paper'], label: 'Paper A' }], edges: [] }
    render(<KnowledgeGraph graph={graph} />)

    ;(await screen.findByRole('button', { name: 'Paper A' })).focus()
    await user.keyboard(' ')

    expect(screen.getByRole('status')).toHaveTextContent('No connections in this view')
  })

  describe('deselecting and panning', () => {
    const small: GraphSnapshot = { nodes: [{ id: 'p1', labels: ['Paper'], label: 'Paper A' }], edges: [] }
    const canvas = () => screen.getByRole('img', { name: /knowledge graph/i })
    const pan = () => canvas().querySelector('.graph-pan') as SVGGElement

    it('deselects the node when empty space is clicked', async () => {
      const user = userEvent.setup()
      render(<KnowledgeGraph graph={small} />)
      await user.click(await screen.findByRole('button', { name: 'Paper A' }))
      expect(screen.getByRole('status')).toBeInTheDocument()

      await user.click(canvas())

      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })

    it('keeps the node selected when the node itself is clicked again', async () => {
      const user = userEvent.setup()
      render(<KnowledgeGraph graph={small} />)
      const node = await screen.findByRole('button', { name: 'Paper A' })
      await user.click(node)
      await user.click(node)
      expect(screen.getByRole('status')).toBeInTheDocument()
    })

    it('pans the view when empty space is dragged, without deselecting', async () => {
      const user = userEvent.setup()
      render(<KnowledgeGraph graph={small} />)
      await user.click(await screen.findByRole('button', { name: 'Paper A' }))
      expect(pan().getAttribute('transform')).toBe('translate(0, 0)')

      await user.pointer([
        { keys: '[MouseLeft>]', target: canvas(), coords: { clientX: 10, clientY: 10 } },
        { coords: { clientX: 60, clientY: 40 } },
        { keys: '[/MouseLeft]' },
      ])

      expect(pan().getAttribute('transform')).toBe('translate(50, 30)')
      expect(screen.getByRole('status')).toBeInTheDocument()
    })

    it('resets the view with the Reset view button', async () => {
      const user = userEvent.setup()
      render(<KnowledgeGraph graph={small} />)
      await screen.findByRole('button', { name: 'Paper A' })
      expect(screen.queryByRole('button', { name: 'Reset view' })).not.toBeInTheDocument()
      await user.pointer([
        { keys: '[MouseLeft>]', target: canvas(), coords: { clientX: 0, clientY: 0 } },
        { coords: { clientX: 30, clientY: 20 } },
        { keys: '[/MouseLeft]' },
      ])
      await user.click(screen.getByRole('button', { name: 'Reset view' }))
      expect(pan().getAttribute('transform')).toBe('translate(0, 0)')
    })
  })

  it('says nothing extra when the whole graph fits', () => {
    const graph: GraphSnapshot = { nodes: nodes.slice(0, 5), edges: [], total_nodes: 5, total_edges: 0 }
    render(<KnowledgeGraph graph={graph} />)
    expect(screen.queryByText(/^Showing/)).not.toBeInTheDocument()
  })
})
