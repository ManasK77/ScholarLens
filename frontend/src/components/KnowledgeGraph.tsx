import { useEffect, useMemo, useRef, useState, type PointerEvent } from 'react'
import { forceCenter, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationNodeDatum } from 'd3-force'
import type { GraphEdge, GraphSnapshot } from '../types'

interface PositionedNode extends SimulationNodeDatum {
  id: string
  labels: string[]
  label: string | null
}

interface KnowledgeGraphProps {
  graph: GraphSnapshot
}

// The canvas stays legible up to about this many nodes.
const MAX_DRAWN_NODES = 180
const WIDTH = 960
const HEIGHT = 520
// Keeps a node's circle inside the viewBox.
const MARGIN = 16
// Pointer travel (px) beyond which a press on empty space is a drag, not a click.
const DRAG_THRESHOLD = 3
// How far the view may be dragged from its home position, in viewBox units.
const MAX_PAN_X = WIDTH / 2
const MAX_PAN_Y = HEIGHT / 2
// Relationships listed for a selected node before "+N more".
const MAX_LISTED = 8

function clamp(value: number | undefined, low: number, high: number) {
  return Math.min(high, Math.max(low, value ?? (low + high) / 2))
}

/** `EVALUATED_USING` → "evaluated using". */
function relationName(type: string) {
  return type.toLowerCase().replaceAll('_', ' ')
}

export default function KnowledgeGraph({ graph }: KnowledgeGraphProps) {
  const [positions, setPositions] = useState<PositionedNode[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const drag = useRef<{ startX: number; startY: number; originX: number; originY: number; moved: boolean } | null>(null)
  const suppressClick = useRef(false)
  const nodeMap = useMemo(() => new Map(positions.map((node) => [node.id, node])), [positions])
  useEffect(() => {
    const nodes: PositionedNode[] = graph.nodes.slice(0, MAX_DRAWN_NODES).map((node) => ({ ...node }))
    const allowed = new Set(nodes.map((node) => node.id))
    const links = graph.edges.filter((edge) => allowed.has(edge.source) && allowed.has(edge.target)).map((edge) => ({ ...edge }))
    const simulation = forceSimulation(nodes)
      .force('link', forceLink<PositionedNode, { source: string; target: string }>(links).id((node) => node.id).distance(72).strength(0.5))
      .force('charge', forceManyBody().strength(-125))
      .force('center', forceCenter(WIDTH / 2, HEIGHT / 2))
      // A gentle pull toward the middle so a large graph does not spread past
      // the canvas, and a hard bound for whatever still would.
      .force('x', forceX(WIDTH / 2).strength(0.05))
      .force('y', forceY(HEIGHT / 2).strength(0.12))
      .alphaDecay(0.08)
      .on('tick', () => {
        for (const node of nodes) {
          node.x = clamp(node.x, MARGIN, WIDTH - MARGIN)
          node.y = clamp(node.y, MARGIN, HEIGHT - MARGIN)
        }
        setPositions(nodes.map((node) => ({ ...node })))
      })
    return () => { simulation.stop() }
  }, [graph])

  const onPointerDown = (event: PointerEvent<SVGSVGElement>) => {
    // Presses on a node belong to the node (select), not to panning.
    if (event.button !== 0 || (event.target as Element).closest('.graph-node')) return
    drag.current = { startX: event.clientX, startY: event.clientY, originX: pan.x, originY: pan.y, moved: false }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }
  const onPointerMove = (event: PointerEvent<SVGSVGElement>) => {
    const start = drag.current
    if (!start) return
    const dx = event.clientX - start.startX
    const dy = event.clientY - start.startY
    if (!start.moved && Math.hypot(dx, dy) <= DRAG_THRESHOLD) return
    start.moved = true
    // Screen pixels to viewBox units, so the graph follows the pointer.
    const width = event.currentTarget.getBoundingClientRect().width
    const scale = width > 0 ? WIDTH / width : 1
    setPan({
      x: Math.min(MAX_PAN_X, Math.max(-MAX_PAN_X, start.originX + dx * scale)),
      y: Math.min(MAX_PAN_Y, Math.max(-MAX_PAN_Y, start.originY + dy * scale)),
    })
  }
  const endDrag = () => {
    // The click that follows a drag must not deselect.
    suppressClick.current = drag.current?.moved ?? false
    drag.current = null
  }
  const onCanvasClick = () => {
    if (suppressClick.current) {
      suppressClick.current = false
      return
    }
    setSelected(null)
  }

  if (!graph.nodes.length) return <div className="inline-empty">The knowledge graph is empty. Run the backend pipeline to populate it.</div>

  const visibleEdges = graph.edges.filter((edge) => nodeMap.has(String(edge.source)) && nodeMap.has(String(edge.target)))
  const selectedNode = selected ? nodeMap.get(selected) : undefined
  const incident: GraphEdge[] = selected ? visibleEdges.filter((edge) => edge.source === selected || edge.target === selected) : []
  const neighbours = new Set(incident.flatMap((edge) => [edge.source, edge.target]))
  const drawn = Math.min(graph.nodes.length, MAX_DRAWN_NODES)
  const total = graph.total_nodes ?? graph.nodes.length
  const nameOf = (id: string) => nodeMap.get(id)?.label || 'Unnamed node'

  return (
    <>
      <div className="graph-wrap">
        <svg className="graph-canvas" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Interactive paper and entity knowledge graph"
          onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={endDrag} onPointerCancel={endDrag} onClick={onCanvasClick}>
          <g className="graph-pan" transform={`translate(${pan.x}, ${pan.y})`}>
          <g className="graph-edges">
            {visibleEdges.map((edge, index) => {
              const source = nodeMap.get(String(edge.source))
              const target = nodeMap.get(String(edge.target))
              if (!source || !target) return null
              const active = selected !== null && (edge.source === selected || edge.target === selected)
              return <line key={`${edge.source}-${edge.target}-${index}`} className={active ? 'active' : undefined} x1={source.x} y1={source.y} x2={target.x} y2={target.y} />
            })}
          </g>
          <g>
            {positions.map((node) => {
              const isPaper = node.labels.includes('Paper')
              const isSelected = selected === node.id
              const name = node.label || 'Unnamed node'
              return (
                <g
                  key={node.id}
                  className="graph-node"
                  transform={`translate(${node.x ?? 0}, ${node.y ?? 0})`}
                  onClick={(event) => { event.stopPropagation(); setSelected(node.id) }}
                  role="button"
                  aria-label={name}
                  aria-pressed={isSelected}
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      setSelected(node.id)
                    }
                  }}
                >
                  <circle r={isSelected ? 12 : isPaper ? 9 : 7} className={isPaper ? 'paper-node' : 'entity-node'} />
                  {(isSelected || neighbours.has(node.id) || positions.length < 35) && <text x="13" y="4">{name}</text>}
                </g>
              )
            })}
          </g>
          </g>
        </svg>
        {(pan.x !== 0 || pan.y !== 0) && <button type="button" className="secondary-button graph-reset" onClick={() => setPan({ x: 0, y: 0 })}>Reset view</button>}
        <div className="graph-legend"><span><i className="paper-swatch" /> Paper</span><span><i className="entity-swatch" /> Entity</span></div>
        {selectedNode && (
          <div className="graph-selection" role="status">
            <span>{selectedNode.labels.join(' · ')}</span>
            <strong>{selectedNode.label || 'Unnamed node'}</strong>
            <small>{incident.length ? `${incident.length} ${incident.length === 1 ? 'connection' : 'connections'} in this view` : 'No connections in this view'}</small>
            {incident.length > 0 && (
              <ul>
                {incident.slice(0, MAX_LISTED).map((edge, index) => (
                  <li key={`${edge.source}-${edge.target}-${index}`}>
                    {edge.source === selected
                      ? `${relationName(edge.type)} ${nameOf(edge.target)}`
                      : `${nameOf(edge.source)} ${relationName(edge.type)} this`}
                  </li>
                ))}
                {incident.length > MAX_LISTED && <li>+{incident.length - MAX_LISTED} more</li>}
              </ul>
            )}
          </div>
        )}
      </div>
      {total > drawn && <p className="subtle">Showing {drawn} of {total} nodes, papers first, then the most connected entities.</p>}
    </>
  )
}
