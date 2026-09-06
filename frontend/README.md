# ScholarLens frontend

The React dashboard exposes the complete evidence-backed research workflow
without requiring direct API access. Its **Add papers** workspace can discover
research from arXiv, Semantic Scholar, and ACL Anthology, accept multi-PDF
uploads, run the evidence pipeline, and display the local corpus. It also
includes ranked Mode A reports, longitudinal trends, an interactive Neo4j
snapshot, and Mode B grounded Q&A.

## Local development

Start the backend on port 8000, then run:

```bash
npm install
npm run dev
```

The Vite development server proxies `/api` to `http://127.0.0.1:8000`. Use **Connect API** in the dashboard and enter the `API_KEY` from the repository-level `.env`. The key is stored only in the current browser tab.

Set `VITE_API_BASE_URL` when the API is hosted elsewhere.

## Verification

```bash
npm test
npm run build
npm run lint
```
