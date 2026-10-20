"""
SearchLocal Embeddings & Hybrid Search Engine

Provides local vector embeddings via Ollama (nomic-embed-text), packed binary vector
storage for SQLite, vectorized cosine similarity, and Reciprocal Rank Fusion (RRF).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Sequence

try:
    import numpy as np
except ImportError:
    np = None


DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


def get_embedding(
    text: str,
    model: str = DEFAULT_EMBED_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
) -> list[float] | None:
    """Fetch vector embedding for a single text using Ollama."""
    res = get_batch_embeddings([text], model=model, base_url=base_url)
    return res[0] if res and len(res) > 0 else None


def get_batch_embeddings(
    texts: Sequence[str],
    model: str = DEFAULT_EMBED_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
    batch_size: int = 32,
) -> list[list[float]] | None:
    """Fetch vector embeddings for a list of texts in batches using Ollama."""
    if not texts:
        return []

    url = f"{base_url.rstrip('/')}/api/embed"
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = list(texts[i : i + batch_size])
        payload = {"model": model, "input": batch}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                embeddings = data.get("embeddings", [])
                if not embeddings:
                    # Fallback check for single 'embedding' field
                    if "embedding" in data:
                        embeddings = [data["embedding"]]
                all_embeddings.extend(embeddings)
        except urllib.error.URLError:
            # Fallback to older /api/embeddings endpoint (one at a time)
            fallback_embeddings = []
            old_url = f"{base_url.rstrip('/')}/api/embeddings"
            for t in batch:
                old_payload = {"model": model, "prompt": t}
                old_req = urllib.request.Request(
                    old_url,
                    data=json.dumps(old_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(old_req, timeout=30) as old_resp:
                        old_data = json.loads(old_resp.read().decode("utf-8"))
                        if "embedding" in old_data:
                            fallback_embeddings.append(old_data["embedding"])
                        else:
                            return None
                except urllib.error.URLError:
                    return None
            all_embeddings.extend(fallback_embeddings)

    return all_embeddings if len(all_embeddings) == len(texts) else None


def pack_embedding(vector: list[float] | Sequence[float]) -> bytes:
    """Pack float array into binary bytes."""
    if np is not None:
        return np.array(vector, dtype=np.float32).tobytes()
    import struct
    return struct.pack(f"{len(vector)}f", *vector)


def unpack_embedding(blob: bytes) -> np.ndarray | list[float]:
    """Unpack binary bytes into float array."""
    if np is not None:
        return np.frombuffer(blob, dtype=np.float32)
    import struct
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def compute_cosine_similarities(
    query_vec: np.ndarray | list[float],
    matrix: np.ndarray | list[np.ndarray],
) -> np.ndarray:
    """Compute cosine similarities between a query vector and a matrix of document vectors."""
    if np is not None:
        q = np.asarray(query_vec, dtype=np.float32)
        m = np.asarray(matrix, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        m_norms = np.linalg.norm(m, axis=1)
        # Avoid division by zero
        denom = m_norms * q_norm
        denom[denom == 0] = 1e-9
        return np.dot(m, q) / denom
    else:
        import math
        q_norm = math.sqrt(sum(x * x for x in query_vec)) or 1e-9
        scores = []
        for doc in matrix:
            dot = sum(a * b for a, b in zip(query_vec, doc))
            d_norm = math.sqrt(sum(x * x for x in doc)) or 1e-9
            scores.append(dot / (q_norm * d_norm))
        return scores


def reciprocal_rank_fusion(
    bm25_results: list[tuple[str, int]],
    semantic_results: list[tuple[str, int]],
    k: int = 60,
    limit: int = 10,
) -> list[tuple[str, int, float]]:
    """
    Combines ranked lists from BM25 and Semantic search using Reciprocal Rank Fusion (RRF).
    RRF Score(d) = sum(1.0 / (k + rank_i))
    """
    scores: dict[tuple[str, int], float] = {}

    # 1. Score BM25 ranks
    for rank, (path, chunk_idx) in enumerate(bm25_results, 1):
        key = (path, chunk_idx)
        scores[key] = scores.get(key, 0.0) + (1.0 / (k + rank))

    # 2. Score Semantic ranks
    for rank, (path, chunk_idx) in enumerate(semantic_results, 1):
        key = (path, chunk_idx)
        scores[key] = scores.get(key, 0.0) + (1.0 / (k + rank))

    # Sort descending by fused score
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [(path, chunk_idx, score) for (path, chunk_idx), score in ranked[:limit]]
