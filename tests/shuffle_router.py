#!/usr/bin/env python3
"""Deterministic shuffle-sharding router matching the Proxy-Wasm Rust implementation."""

from __future__ import annotations

import math
from typing import Sequence

MASK_64 = 0xFFFFFFFFFFFFFFFF


def fnv1a_64(data: bytes, seed: int) -> int:
    """Exact 64-bit FNV-1a + fmix64 avalanche finalizer matching router-wasm/src/lib.rs."""
    h = 0xCBF29CE484222325
    for byte in data:
        h ^= byte
        h = (h * 0x100000001B3) & MASK_64
    h = (h ^ ((seed + 0x9E3779B97F4A7C15) & MASK_64)) & MASK_64
    h ^= h >> 33
    h = (h * 0xFF51AFD7ED558CCD) & MASK_64
    h ^= h >> 33
    h = (h * 0xC4CEB9FE1A85EC53) & MASK_64
    h ^= h >> 33
    return h


def compute_virtual_shard(
    tenant_id: str,
    total_cells: int = 8,
    shard_size: int = 3,
    salt: str = "gcp-cell-v1-prod",
) -> list[int]:
    """Computes a tenant's k-of-N virtual shard ('hand of cards') via partial Fisher-Yates."""
    if not (1 <= shard_size <= total_cells):
        raise ValueError(f"Invalid shard_size={shard_size} for total_cells={total_cells}")
    deck = list(range(total_cells))
    key_bytes = f"{salt}:{tenant_id}".encode("utf-8")
    for i in range(shard_size):
        remaining = total_cells - i
        pick = i + (fnv1a_64(key_bytes, i) % remaining)
        deck[i], deck[pick] = deck[pick], deck[i]
    return deck[:shard_size]


def blast_radius_stats(total_cells: int, shard_size: int) -> dict[str, float]:
    """Returns combinatorial blast-radius isolation metrics for (N, k)."""
    virtual_shards = math.comb(total_cells, shard_size)
    single_cell_pct = 100.0 / total_cells
    shuffle_overlap_pct = 100.0 / virtual_shards
    improvement_factor = single_cell_pct / shuffle_overlap_pct
    return {
        "total_cells": total_cells,
        "shard_size": shard_size,
        "virtual_shards": virtual_shards,
        "single_cell_blast_radius_pct": round(single_cell_pct, 2),
        "shuffle_blast_radius_pct": round(shuffle_overlap_pct, 2),
        "isolation_improvement_x": round(improvement_factor, 1),
    }


def route_with_failover(
    hand: Sequence[int],
    healthy_cells: set[int],
) -> int | None:
    """Selects the first healthy cell in a tenant's virtual shard (or None if all crashed)."""
    for cell_idx in hand:
        if cell_idx in healthy_cells:
            return cell_idx
    return None
