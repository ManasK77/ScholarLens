"""Read-only. Encode each evaluation question with the production encoder (cached
model, Hugging Face forced offline) and read the top-100 cosine hits from Qdrant.

Run inside the backend container, which has the model cache and the index:

    docker cp deep_in.json nlp-backend-1:/tmp/deep_in.json
    docker exec -w /app -e PYTHONPATH=/app nlp-backend-1 python encode_and_retrieve.py

`deep_in.json` is `[{"id": "p001", "text": "..."}, ...]` from the frozen
`questions.csv`. No write to any store, no LLM, no download.
"""

import json
import os
import sys
import time

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from app.core.config import get_settings
from app.embeddings.indexer import EvidenceIndex

with open("/tmp/deep_in.json", encoding="utf-8") as handle:
    questions = json.load(handle)
index = EvidenceIndex(get_settings())
out = {}
start = time.time()
for item in questions:
    hits = index.retrieve(item["text"], 100)
    out[item["id"]] = [[h["id"], h["paper_id"], round(h["score"], 6)] for h in hits]
print(json.dumps(out))
print("secs", round(time.time() - start, 1), "questions", len(out), file=sys.stderr)
