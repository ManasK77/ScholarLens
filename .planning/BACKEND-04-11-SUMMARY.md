# Backend completion summary — Phases 4–11

Date: 2026-09-01

The owner explicitly authorized all remaining backend work while deferring the
frontend. Phases 4–11 were therefore implemented in dependency order as one
continuous backend pass, with tests written for each stage before its service
implementation.

## Delivered

- Phase 4: GLiNER2.5 `JointIE` entity/relation extraction and idempotent Neo4j
  graph persistence/querying.
- Phase 5: lazy SPECTER2 embeddings, Qdrant indexing, filtered semantic search,
  and Neo4j paper-node linkage in every point payload.
- Phase 6: scikit-learn HDBSCAN clustering with preserved noise singletons,
  DeBERTa-MNLI contradiction checks, and PostgreSQL cluster persistence.
- Phase 7: publication-year count series and NumPy trend slopes.
- Phase 8: configurable five-signal scoring with stored signal breakdowns;
  Semantic Scholar citation counts now survive ingestion for citation impact.
- Phase 9: single-pass grounded Mode A reports using direct structured-output
  LLM calls.
- Phase 10: bounded LangGraph GraphRAG with vector+graph retrieval, sufficiency
  assessment, capped reformulation, cross-encoder reranking, generation, and
  structural plus semantic grounding checks.
- Phase 11: API-key-protected OpenAPI surface for papers, graph, index,
  clusters, trends, gaps, reports, queries, and full pipeline execution.
- LLM providers: `LLM_PROVIDER=gemini` via Gemini Interactions API or
  `LLM_PROVIDER=ollama` via the local `/api/chat` structured-output endpoint.
  An optional Compose `ollama` profile is included.

## Verification

- 143 default backend tests pass; 3 resource-intensive integration tests remain
  opt-in.
- Ruff check and format check pass.
- `git diff --check` passes.
- `uv pip check` reports all 136 installed packages compatible.
- FastAPI OpenAPI inspection confirms every required endpoint and the
  `X-API-Key` security scheme.
- `docker compose --env-file .env.example config --quiet` passes.

## Review findings addressed

- Resolved the GLiNER2/Transformers dependency conflict by keeping the shared
  stack on Transformers 4.x and sentence-transformers below 6.
- GLiNER2 joint relations use internal entity IDs; the adapter resolves those
  IDs to typed surface entities before graph writes.
- API authentication now fails closed because `API_KEY` is required.
- Added PDF magic-byte validation and per-request upload-count bounds.
- Provider exceptions are logged server-side without exposing internal details
  in HTTP responses.
- Docker installs CPU-only PyTorch wheels to avoid accidental CUDA downloads.
- Gemini REST parsing handles the real Interactions `steps/model_output` shape,
  not only SDK convenience fields.

## Environmental follow-up

Docker Desktop was started for the live image/service acceptance check, but its
daemon never became reachable at the configured socket. The Compose model is
syntactically valid and all service/model integrations have offline tests, but
the backend image build and real Neo4j/Qdrant/Postgres/GROBID/model smoke run
still need to be executed once Docker is healthy.

No frontend source file was changed during this backend pass. The pre-existing
`frontend/package-lock.json` working-tree modification was preserved.
