"""Read-only. Score each answerable question's top-100 cosine pool with the
production cross-encoder (cached model, Hugging Face forced offline).

Run inside the backend container. `/tmp/ce2_in.json` is
`[{"id": "p001", "text": "<question>", "pool": [["<passage id>", "<passage text>"], ...]}]`
built from `cosine-top100.json` and the frozen passage catalogue. The output is
`{question id: {passage id: logit}}`. No write to any store, no LLM, no download.
"""

import json
import os
import sys
import time

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
from sentence_transformers import CrossEncoder

with open("/tmp/ce2_in.json", encoding="utf-8") as handle:
    items = json.load(handle)
model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
out = {}
start = time.time()
for item in items:
    scores = model.predict([(item["text"], p[1]) for p in item["pool"]], batch_size=32)
    out[item["id"]] = {
        p[0]: float(x) for p, x in zip(item["pool"], scores, strict=True)
    }
print(json.dumps(out))
print("secs", round(time.time() - start, 1), file=sys.stderr)
