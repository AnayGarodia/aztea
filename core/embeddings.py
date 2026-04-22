"""
embeddings.py — text embedding helpers using the OpenAI API.
"""

from __future__ import annotations

import os

import numpy as np

_MODEL_NAME = "text-embedding-3-small"
EMBEDDING_DIM = 384


def embed_text(text: str) -> list[float]:
    normalized = str(text or "").strip()
    if not normalized:
        raise ValueError("text must be a non-empty string.")

    import openai  # deferred so tests that don't set OPENAI_API_KEY can import this module

    api_key = os.environ.get("OPENAI_API_KEY")
    client = openai.OpenAI(api_key=api_key)
    response = client.embeddings.create(
        model=_MODEL_NAME,
        input=normalized,
        dimensions=EMBEDDING_DIM,
    )
    vector = response.data[0].embedding
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    if arr.size != EMBEDDING_DIM:
        raise RuntimeError(
            f"Expected embedding dimension {EMBEDDING_DIM}, got {arr.size} from model '{_MODEL_NAME}'."
        )
    return arr.tolist()


def cosine(a: list[float] | np.ndarray, b: list[float] | np.ndarray) -> float:
    arr_a = np.asarray(a, dtype=np.float32).reshape(-1)
    arr_b = np.asarray(b, dtype=np.float32).reshape(-1)
    if arr_a.size == 0 or arr_b.size == 0:
        raise ValueError("cosine inputs must be non-empty vectors.")
    if arr_a.size != arr_b.size:
        raise ValueError("cosine inputs must have the same dimensionality.")

    norm_a = float(np.linalg.norm(arr_a))
    norm_b = float(np.linalg.norm(arr_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(arr_a, arr_b) / (norm_a * norm_b))
