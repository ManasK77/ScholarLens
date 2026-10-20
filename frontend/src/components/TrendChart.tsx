import createPlotlyComponent from 'react-plotly.js/factory'
import Plotly from 'plotly.js-basic-dist-min'
import type { Cluster, Trend } from '../types'

const Plot = createPlotlyComponent(Plotly)

interface TrendChartProps {
  trends: Trend[]
  clusters: Cluster[]
}

export default function TrendChart({ trends, clusters }: TrendChartProps) {
  const clusterNames = new Map(clusters.map((cluster) => [cluster.id, cluster.label || `Cluster ${cluster.id}`]))
  const strongest = [...trends].sort((a, b) => Math.abs(b.slope) - Math.abs(a.slope)).slice(0, 8)
  const traces = strongest.map((trend, index) => {
    const years = Object.keys(trend.yearly_counts).map(Number).sort((a, b) => a - b)
    const colors = ['#087b6d', '#e99b4a', '#345c8a', '#a55a73', '#6d7c44', '#6b5e91', '#bb6b43', '#397d78']
    return {
      x: years,
      y: years.map((year) => trend.yearly_counts[String(year)] ?? 0),
      type: 'scatter' as const,
      mode: 'lines+markers' as const,
      name: clusterNames.get(trend.cluster_id),
      line: { width: 3, color: colors[index % colors.length] },
      marker: { size: 7 },
      // The series counts distinct papers per year, not sentences or mentions.
      hovertemplate: '%{y} papers in %{x}<extra>%{fullData.name}</extra>',
    }
  })

  if (!traces.length) return <div className="inline-empty">No trend series have been computed yet.</div>

  return (
    <Plot
      data={traces}
      layout={{
        autosize: true,
        height: 470,
        margin: { l: 48, r: 20, t: 20, b: 58 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { family: 'Manrope, sans-serif', color: '#50615b', size: 12 },
        xaxis: { title: { text: 'Publication year' }, gridcolor: '#e6e7df', zeroline: false, dtick: 1 },
        yaxis: { title: { text: 'Papers per year' }, gridcolor: '#e6e7df', zeroline: false, rangemode: 'tozero' },
        legend: { orientation: 'h', y: -0.22, x: 0 },
        hoverlabel: { bgcolor: '#102b27', bordercolor: '#102b27', font: { color: '#fff' } },
      }}
      config={{ responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'] }}
      useResizeHandler
      style={{ width: '100%' }}
    />
  )
}
