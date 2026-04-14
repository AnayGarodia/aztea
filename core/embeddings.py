"""
embeddings.py — local text embedding helpers for semantic registry matching.
"""

from __future__ import annotations

import threading

import numpy as np
from sentence_transformers import SentenceTransformer

_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_MODEL_LOCK = threading.Lock()
_MODEL: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                _MODEL = SentenceTransformer(_MODEL_NAME)
    return _MODEL


def embed_text(text: str) -> list[float]:
    normalized = str(text or "").strip()
    if not normalized:
        raise ValueError("text must be a non-empty string.")

    model = _get_model()
    vector = model.encode(normalized, convert_to_numpy=True, normalize_embeddings=False)
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
