"""
app/services/storage.py
=======================
Vectorless RAG — Storage & Caching Layer  (PageIndex-aligned)

Responsibilities:
  - insert_nodes()            : Upsert DocumentNode rows into Supabase
                                (now includes page_start, page_end, path)
  - cache_document_outline()  : Fetch ALL levels → cache full tree in Redis
                                (was L1+L2 only; upgraded to support 1-4)

All I/O is fully async to avoid blocking the WebRTC/WebSocket audio stream.
"""

from __future__ import annotations

import json
import os
import sys

# Ensure stdout can handle arbitrary unicode on Windows (cp1252 console)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import redis.asyncio as aioredis
from supabase._async.client import AsyncClient, create_client

from app.services.ingestion import DocumentNode


# ── Supabase client factory ────────────────────────────────────────────────────


async def _get_supabase() -> AsyncClient:
    url: str = os.environ["SUPABASE_URL"].strip()
    key: str = os.environ["SUPABASE_KEY"].strip()
    return await create_client(url, key)


# ── Redis client factory ───────────────────────────────────────────────────────


def _get_redis() -> aioredis.Redis:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379").strip()
    return aioredis.from_url(redis_url, decode_responses=True)


# ── Supabase: insert nodes ─────────────────────────────────────────────────────


async def insert_nodes(nodes: list[DocumentNode]) -> None:
    """
    Upsert all DocumentNode objects into the `document_nodes` Supabase table.
    Flattens the nested tree structure back into relational rows.
    """
    client = await _get_supabase()

    flat_rows = []

    def _flatten(node: DocumentNode):
        flat_rows.append(
            {
                "node_id": node.node_id,
                "document_id": node.document_id,
                "title": node.title,
                "level": node.level,
                "parent_id": node.parent_id,
                "path": node.path,
                "summary": node.summary,
                "content": node.content,
                "start_index": node.start_index,
                "end_index": node.end_index,
                "page_start": node.page_start,
                "page_end": node.page_end,
            }
        )
        for child in node.nodes:
            _flatten(child)

    for root in nodes:
        _flatten(root)

    # Batch upsert — idempotent if run multiple times
    response = (
        await client.table("document_nodes")
        .upsert(flat_rows, on_conflict="node_id")
        .execute()
    )

    print(
        f"[storage] Upserted {len(flat_rows)} nodes -> Supabase (levels: {sorted(set(n['level'] for n in flat_rows))})"
    )


# ── Redis: cache document outline (full tree) ──────────────────────────────────

_OUTLINE_TTL_SECONDS = 3600  # 1 hour


async def cache_document_outline(document_id: str) -> None:
    """
    Fetch ALL level nodes (1-4) from Supabase for `document_id`
    and cache them as a compact JSON tree in Redis.

    Upgrade from original: previously only cached L1+L2.
    Now caches all levels so the tree search can traverse deeper nodes.

    Redis key: `outline:{document_id}`
    TTL: 1 hour

    Each cached item includes:
      { node_id, title, level, parent_id, path, summary, page_start, page_end }
    """
    client = await _get_supabase()

    # Fetch all levels, ordered by level then by creation (preserves document order)
    response = (
        await client.table("document_nodes")
        .select(
            "node_id, title, level, parent_id, path, summary, start_index, end_index, page_start, page_end"
        )
        .eq("document_id", document_id)
        .order("created_at")
        .execute()
    )

    outline = response.data  # list of dicts
    print(
        f"[storage] Fetched {len(outline)} outline nodes across levels {sorted(set(n['level'] for n in outline))}"
    )

    redis = _get_redis()
    try:
        redis_key = f"outline:{document_id}"
        await redis.set(redis_key, json.dumps(outline), ex=_OUTLINE_TTL_SECONDS)
        print(
            f"[storage] Cached outline → Redis key '{redis_key}' (TTL={_OUTLINE_TTL_SECONDS}s)"
        )
    finally:
        await redis.aclose()
