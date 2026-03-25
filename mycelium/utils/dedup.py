"""Deterministic dedup: exact name match + cosine threshold."""

from __future__ import annotations

from mycelium.prompts.ingest import NeuronContext


def normalize(name: str) -> str:
    return name.strip().lower()


def exact_match(
    name:     str,
    existing: list[NeuronContext],
) -> NeuronContext | None:
    norm = normalize(name)
    for e in existing:
        if normalize(e.name) == norm:
            return e
    return None


def find_exact_matches(
    names:    list[str],
    existing: list[NeuronContext],
) -> dict[str, str]:
    """Batch exact match: {extracted_name: existing_uuid}."""
    index = {normalize(e.name): e.uuid for e in existing}
    return {
        name: index[normalize(name)]
        for name in names
        if normalize(name) in index
    }


def cosine_sim(a: list[float], b: list[float]) -> float:
    """Dot product (= cosine for L2-normalized vectors). P0.6."""
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False))
