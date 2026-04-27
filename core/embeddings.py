"""
embeddings.py — text embedding helpers.

Priority order when OPENAI_API_KEY is not set:
  1. sentence-transformers (local, no API key needed, semantically meaningful)
  2. Random fallback — only when AZTEA_DISABLE_EMBEDDINGS=1, for environments
     where even the local model is unavailable (e.g. minimal CI runners).

Callers that receive a zero-vector or near-zero cosine score should check
feature_flags.DISABLE_EMBEDDINGS and skip the semantic contribution rather
than blending in noise.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer as _SentenceTransformerType

logger = logging.getLogger(__name__)

_LOCAL_MODEL_NAME = "all-MiniLM-L6-v2"
_OAI_MODEL_NAME = "text-embedding-3-small"
EMBEDDING_DIM = 384


@lru_cache(maxsize=1)
def _local_model() -> "_SentenceTransformerType | None":
    """Load sentence-transformers model once per process.  Returns None on failure."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import]
        model = SentenceTransformer(_LOCAL_MODEL_NAME)
        logger.debug("embeddings: loaded local model %s", _LOCAL_MODEL_NAME)
        return model
    except Exception as exc:  # pragma: no cover
        logger.warning("embeddings: sentence-transformers unavailable (%s); falling back to disabled mode", exc)
        return None


def _random_embed(text: str) -> list[float]:
    """Deterministic random vector — NOT semantically meaningful.

    Used only when AZTEA_DISABLE_EMBEDDINGS=1.  Cosine similarity between two
    random vectors converges to 0 as dimension grows, so search scores will be
    near-zero; callers should skip the semantic term entirely in that case.
    """
    import hashlib
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

    # --- OpenAI path (when API key is configured) ---
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        import openai
        client = openai.OpenAI(api_key=api_key)
        response = client.embeddings.create(
            model=_OAI_MODEL_NAME,
            input=normalized,
            dimensions=EMBEDDING_DIM,
        )
        vector = response.data[0].embedding
        arr = np.asarray(vector, dtype=np.float32).reshape(-1)
        if arr.size != EMBEDDING_DIM:
            raise RuntimeError(
                f"Expected embedding dimension {EMBEDDING_DIM}, got {arr.size} from model '{_OAI_MODEL_NAME}'."
            )
        return arr.tolist()

    # --- Explicit disable (CI / minimal envs) ---
    from core.feature_flags import DISABLE_EMBEDDINGS
    if DISABLE_EMBEDDINGS:
        return _random_embed(normalized)

    # --- Local sentence-transformers (default offline path) ---
    model = _local_model()
    if model is not None:
        vec = model.encode(normalized, normalize_embeddings=True)
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        # Pad or truncate to EMBEDDING_DIM if model uses a different dimension.
        if arr.size != EMBEDDING_DIM:
            padded = np.zeros(EMBEDDING_DIM, dtype=np.float32)
            copy_len = min(arr.size, EMBEDDING_DIM)
            padded[:copy_len] = arr[:copy_len]
            arr = padded
            norm = float(np.linalg.norm(arr))
            if norm > 0:
                arr /= norm
        return arr.tolist()

    # Absolute last resort: disable silently.
    logger.warning("embeddings: all embedding backends unavailable; returning zero vector")
    return [0.0] * EMBEDDING_DIM


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
