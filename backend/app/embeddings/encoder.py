"""Lazy SPECTER2 encoder used for indexing, retrieval, and clustering."""

from functools import lru_cache

import numpy as np

from app.core.config import get_settings


class Specter2Encoder:
    def __init__(self, model_name: str) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(model_name)
        self._model = AutoModel.from_pretrained(model_name)
        self._model.eval()
        self.dimension = int(self._model.config.hidden_size)

    def encode(self, texts: list[str], batch_size: int = 16) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            tokens = self._tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            with self._torch.inference_mode():
                # SPECTER2 uses the first-token representation for document and
                # paragraph retrieval embeddings.
                embeddings = self._model(**tokens).last_hidden_state[:, 0, :]
                embeddings = self._torch.nn.functional.normalize(embeddings, p=2, dim=1)
            vectors.extend(embeddings.cpu().numpy().astype(np.float32).tolist())
        return vectors


@lru_cache(maxsize=1)
def get_encoder() -> Specter2Encoder:
    return Specter2Encoder(get_settings().specter2_model)
