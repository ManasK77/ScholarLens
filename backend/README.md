# ScholarLens backend

The backend implements the complete ScholarLens pipeline from literature
collection through grounded research-gap reports and interactive GraphRAG.

## Configuration

Copy the repository-level `.env.example` to `.env`, replace every placeholder,
and choose one LLM provider.

Gemini:

```dotenv
LLM_PROVIDER=gemini
GEMINI_API_KEY=your-google-ai-studio-key
GEMINI_MODEL=gemini-2.5-flash
```

Ollama:

```dotenv
LLM_PROVIDER=ollama
OLLAMA_URL=http://ollama:11434
OLLAMA_MODEL=qwen3:8b
```

For Docker-based Ollama, start the optional profile and pull the configured
model once:

```bash
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull qwen3:8b
```

Use the exact model named by `OLLAMA_MODEL`. For a 6–8B model, give Docker
enough memory for Ollama and the roughly 2 GB GROBID service to coexist, or stop
GROBID after ingestion while running interactive queries.

All non-health API routes require the configured key:

```http
X-API-Key: your-API_KEY-value
```

## Running

With infrastructure running, start the API from `backend/`:

```bash
uv run --extra dev uvicorn app.main:app --reload
```

OpenAPI documentation is available at `http://localhost:8000/docs`.

The first real execution of GLiNER2, SPECTER2, DeBERTa-NLI, or the cross-encoder
downloads that model from Hugging Face. Models are lazy-loaded and then reused
for the life of the process.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/collect` | Discover and deduplicate papers for a topic |
| `POST` | `/upload` | Add owner-supplied PDFs |
| `GET` | `/papers` | Browse/filter normalized corpus records |
| `POST/GET` | `/graph` | Build or inspect the Neo4j knowledge graph |
| `POST` | `/index` | Build the SPECTER2 Qdrant evidence index |
| `POST/GET` | `/clusters` | Rebuild or inspect semantic clusters and contradictions |
| `POST/GET` | `/trends` | Rebuild or inspect yearly cluster trends |
| `POST/GET` | `/gaps` | Rank or browse gap candidates and signal breakdowns |
| `POST/GET` | `/reports` | Generate or browse Mode A grounded reports |
| `POST` | `/query` | Ask a Mode B GraphRAG question |
| `POST` | `/pipeline/run` | Run parsing through report generation in dependency order |

`POST /pipeline/run` accepts `{"generate_reports": false}` when the structured
pipeline should run without invoking Gemini or Ollama.

`POST /query` searches the local corpus; it does not perform web discovery. Run
`POST /collect` followed by `POST /pipeline/run` first. When Qdrant contains no
evidence points, the endpoint returns an explicit ungrounded response without
loading retrieval models or calling the LLM.

## Verification

```bash
uv run --extra dev pytest -q
uv run --extra dev ruff check app tests
uv run --extra dev ruff format --check app tests
```

The default test suite mocks external APIs and heavyweight model inference.
Opt-in integration tests are marked `integration`.
