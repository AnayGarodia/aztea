"""
embeddings.py — text embedding helpers using the OpenAI API.

When OPENAI_API_KEY is not set (CI, local dev without a key), embed_text falls
back to a deterministic hash-seeded vector so tests and offline tooling work
without any external API call. The fallback is not semantically meaningful but
is consistent: the same text always returns the same vector.
"""

from __future__ import annotations

import hashlib
import os

import numpy as np

_MODEL_NAME = "text-embedding-3-small"
EMBEDDING_DIM = 384


def _fallback_embed(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:4], "big")
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


def embed_text(text: str) -> list[float]:
    normalized = str(text or "").strip()
    if not normalized:
        raise ValueError("text must be a non-empty string.")

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return _fallback_embed(normalized)

    import openai

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
