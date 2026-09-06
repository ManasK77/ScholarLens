declare module 'plotly.js-basic-dist-min' {
  import type { PlotlyHTMLElement } from 'plotly.js'
  const Plotly: typeof import('plotly.js') & { PlotlyHTMLElement: PlotlyHTMLElement }
  export default Plotly
}
