import { useEffect, useMemo, useState } from 'react'
import { forceCenter, forceLink, forceManyBody, forceSimulation, type SimulationNodeDatum } from 'd3-force'
import type { GraphSnapshot } from '../types'

interface PositionedNode extends SimulationNodeDatum {
  id: string
  labels: string[]
  label: string | null
}

interface KnowledgeGraphProps {
  graph: GraphSnapshot
}

export default function KnowledgeGraph({ graph }: KnowledgeGraphProps) {
  const [positions, setPositions] = useState<PositionedNode[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const nodeMap = useMemo(() => new Map(positions.map((node) => [node.id, node])), [positions])

  useEffect(() => {
    const nodes: PositionedNode[] = graph.nodes.slice(0, 180).map((node) => ({ ...node }))
    const allowed = new Set(nodes.map((node) => node.id))
    const links = graph.edges.filter((edge) => allowed.has(edge.source) && allowed.has(edge.target)).map((edge) => ({ ...edge }))
    const simulation = forceSimulation(nodes)
      .force('link', forceLink<PositionedNode, { source: string; target: string }>(links).id((node) => node.id).distance(72).strength(0.5))
      .force('charge', forceManyBody().strength(-125))
      .force('center', forceCenter(480, 255))
      .alphaDecay(0.08)
      .on('tick', () => setPositions(nodes.map((node) => ({ ...node }))))
    return () => { simulation.stop() }
  }, [graph])

  if (!graph.nodes.length) return <div className="inline-empty">The knowledge graph is empty. Run the backend pipeline to populate it.</div>

  const visibleEdges = graph.edges.filter((edge) => nodeMap.has(String(edge.source)) && nodeMap.has(String(edge.target)))
  const selectedNode = selected ? nodeMap.get(selected) : undefined

  return (
    <div className="graph-wrap">
      <svg className="graph-canvas" viewBox="0 0 960 520" role="img" aria-label="Interactive paper and entity knowledge graph">
        <g className="graph-edges">
          {visibleEdges.map((edge, index) => {
            const source = nodeMap.get(String(edge.source))
            const target = nodeMap.get(String(edge.target))
            if (!source || !target) return null
            return <line key={`${edge.source}-${edge.target}-${index}`} x1={source.x} y1={source.y} x2={target.x} y2={target.y} />
          })}
        </g>
        <g>
          {positions.map((node) => {
            const isPaper = node.labels.includes('Paper')
            const isSelected = selected === node.id
            return (
              <g key={node.id} className="graph-node" transform={`translate(${node.x ?? 0}, ${node.y ?? 0})`} onClick={() => setSelected(node.id)} role="button" tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter') setSelected(node.id) }}>
                <circle r={isSelected ? 12 : isPaper ? 9 : 7} className={isPaper ? 'paper-node' : 'entity-node'} />
                {(isSelected || positions.length < 35) && <text x="13" y="4">{node.label || 'Unnamed node'}</text>}
              </g>
            )
          })}
        </g>
      </svg>
      <div className="graph-legend"><span><i className="paper-swatch" /> Paper</span><span><i className="entity-swatch" /> Entity</span></div>
      {selectedNode && <div className="graph-selection"><span>{selectedNode.labels.join(' · ')}</span><strong>{selectedNode.label || 'Unnamed node'}</strong></div>}
    </div>
  )
}
