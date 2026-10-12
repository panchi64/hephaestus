"""Shared hybrid-recall scorers — the brute-force ranking primitives.

These are the small, pure scoring functions behind every "decrypt the owner's
working set, score it two ways, fuse the rankings" recall in this codebase
(long-term memory and cross-chat search). They live here, not inside any one
store, so there is a single implementation of token overlap, cosine similarity,
and Reciprocal Rank Fusion rather than a copy per caller.

Brute-force-in-Python (not an in-DB ANN index) is the deliberate consequence of
encrypting vectors at rest — microseconds at single-operator scale, every vector
sealed. The dense + sparse split satisfies "recall by meaning, keyword fallback"
in one pass: an item with no comparable vector simply contributes via the sparse
signal alone.
"""

from __future__ import annotations

import re
from collections import Counter

import numpy as np

RRF_K = 60  # Reciprocal Rank Fusion constant (standard default)
_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> set[str]:
    """The lowercase alphanumeric token set used for sparse (keyword) overlap."""
    return set(_TOKEN.findall(text.lower()))


def term_counts(text: str) -> Counter[str]:
    """Token frequencies, for BM25."""
    return Counter(_TOKEN.findall(text.lower()))


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity, or 0.0 for mismatched shapes / a zero vector."""
    if a.shape != b.shape:
        return 0.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0


def rrf(dense: dict[str, float], sparse: dict[str, float]) -> dict[str, float]:
    """Reciprocal Rank Fusion of two score maps into one fused score per id."""
    fused: dict[str, float] = {}
    for scores in (dense, sparse):
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        for rank, (mid, _score) in enumerate(ranked, start=1):
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (RRF_K + rank)
    return fused


def rrf_lists(ranked_lists: list[list[str]]) -> dict[str, float]:
    """Reciprocal Rank Fusion of N already-ordered id lists into one fused score
    per id. Each list is one retriever's result, best-first; an id's fused score sums
    ``1 / (RRF_K + position)`` across the lists it appears in. Purely rank-based, so
    lists whose raw scores live on different scales (a memory's fused score vs. a
    folder chunk's cosine) combine without any normalization."""
    fused: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, mid in enumerate(ranked, start=1):
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (RRF_K + rank)
    return fused


def matched_by(mid: str, dense: dict[str, float], sparse: dict[str, float]) -> str:
    """How an id surfaced: ``both``, ``semantic`` (dense only), or ``keyword``."""
    in_dense, in_sparse = mid in dense, mid in sparse
    if in_dense and in_sparse:
        return "both"
    return "semantic" if in_dense else "keyword"
