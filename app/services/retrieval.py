"""
app/services/retrieval.py
=========================
Vectorless RAG — Retrieval Engine  (PageIndex Tree Search aligned)

Flow:
  1. Fetch document outline from Redis (tree-structured, all levels)
  2. LLM Tree Search (Groq): returns a LIST of node_ids ("node_list")
     with explicit "thinking" reasoning — mirrors PageIndex's tree search prompt
  3. Fetch content for ALL matched nodes from Supabase
  4. Synthesize a concise, voice-friendly answer across multi-node context

Key upgrade from the original single-node routing:
  - Returns MULTIPLE nodes (top-K), not just 1
  - Explicit reasoning chain ("thinking") for traceability
  - Answer synthesis step — LLM condenses multi-node content into one answer
  - Deeper tree support (levels 1-4)
"""

from __future__ import annotations

import json
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

import redis.asyncio as aioredis
from groq import AsyncGroq
from pydantic import BaseModel
from supabase._async.client import AsyncClient, create_client


# ── Pydantic structured-output model — PageIndex tree search style ────────────


class NodeRoute(BaseModel):
    """
    Multi-node routing result — mirrors PageIndex's tree search output format:
      { "thinking": <reasoning>, "node_list": [node_id1, node_id2, ...] }
    """

    thinking: str  # LLM's reasoning chain (why these nodes?)
    node_list: list[str]  # ordered list of relevant node_ids (most relevant first)


# ── Shared client helpers ──────────────────────────────────────────────────────


async def _get_supabase() -> AsyncClient:
    return await create_client(
        os.environ["SUPABASE_URL"].strip(),
        os.environ["SUPABASE_KEY"].strip(),
    )


def _get_redis() -> aioredis.Redis:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379").strip()
    return aioredis.from_url(redis_url, decode_responses=True)


# ── Step 1: Fetch tree outline from Redis ─────────────────────────────────────


async def _fetch_outline(document_id: str) -> list[dict]:
    """
    Retrieve the cached outline JSON from Redis.
    Falls back to empty list if key missing.
    """
    redis = _get_redis()
    try:
        raw = await redis.get(f"outline:{document_id}")
        if raw is None:
            print(f"[retrieval] WARNING: No outline cached for '{document_id}'.")
            return []
        return json.loads(raw)
    finally:
        await redis.aclose()


# ── Step 2: LLM Tree Search — returns node_list[] ────────────────────────────


async def _tree_search(user_query: str, outline: list[dict]) -> list[str]:
    """
    Multi-phase LLM drill-down search (Vectorless RAG Tree Retrieval):
      1. Group outline into a parent-children map.
      2. Ask LLM to pick the most relevant Root (Level 1) branches.
      3. For each picked branch, recursively ask LLM to pick among its children.
      4. Return the specific leaf nodes reached.
    """
    if not outline:
        return []

    # 1. Build tree adjacency list
    children_map: dict[str | None, list[dict]] = {}
    by_id: dict[str, dict] = {}
    for n in outline:
        by_id[n["node_id"]] = n
        pid = n.get("parent_id")
        if pid not in children_map:
            children_map[pid] = []
        children_map[pid].append(n)

    # Locate true roots
    roots = children_map.get(None, [])
    if not roots:
        # Fallback if flat
        roots = [n for n in outline if n.get("level", 1) == 1]
    if not roots:
        roots = outline

    final_leaf_ids: list[str] = []
    groq_client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])

    # 2. Helper to ask LLM to pick from a list of siblings
    async def _pick_nodes(
        candidates: list[dict], parent_title: str = "Document Root"
    ) -> list[str]:
        if not candidates:
            return []
        if len(candidates) == 1:
            return [candidates[0]["node_id"]]

        tree_text = "\n".join(
            f"[{n['node_id']}] {n['title']}: {n.get('summary', '')}" for n in candidates
        )

        prompt_system = (
            "You are a retrieval assistant navigating a document tree. "
            "Given a user query and a list of subsections, select ALL section IDs "
            "that likely contain information to answer the query.\n"
            'Return JSON: {"thinking": "<reasoning>", "node_list": ["id1", ...]}'
        )
        prompt_user = (
            f"Query: {user_query}\n\n"
            f"Current Location: {parent_title}\n"
            f"Subsections available:\n{tree_text}"
        )

        try:
            resp = await groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": prompt_system},
                    {"role": "user", "content": prompt_user},
                ],
                max_tokens=200,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content.strip())
            return data.get("node_list", [])
        except Exception as e:
            print(f"[retrieval] Drill-down error at {parent_title}: {e}")
            return [candidates[0]["node_id"]]

    # 3. Recursive exploration function
    async def _explore(node_id: str):
        node = by_id.get(node_id)
        if not node:
            return

        kids = children_map.get(node_id, [])
        if not kids:
            # Reached a leaf node!
            final_leaf_ids.append(node_id)
            return

        print(
            f"[retrieval] Phase 2: Drilling down into '{node['title']}' ({len(kids)} sub-sections)..."
        )
        selected_kids = await _pick_nodes(kids, parent_title=node["title"])

        if not selected_kids:
            # LLM didn't pick any children, but it picked the parent.
            # Stop here and use the parent's content.
            final_leaf_ids.append(node_id)
        else:
            # Recurse into the chosen children
            import asyncio

            tasks = [_explore(kid) for kid in selected_kids if kid in by_id]
            await asyncio.gather(*tasks)

    # 4. Start search at Level 1 Roots
    print(
        f"[retrieval] Phase 1: Checking {len(roots)} root sections for query: '{user_query}'..."
    )
    selected_roots = await _pick_nodes(roots)

    import asyncio

    await asyncio.gather(*[_explore(rid) for rid in selected_roots if rid in by_id])

    # Fallback if nothing was reached
    if not final_leaf_ids:
        print(
            "[retrieval] Warning: tree search returned empty, falling back to top root"
        )
        final_leaf_ids = [roots[0]["node_id"]] if roots else []

    # Dedup and limit
    final_leaves = list(dict.fromkeys(final_leaf_ids))[:4]

    # Debug print the final titles
    titles = [by_id[nid]["title"] for nid in final_leaves if nid in by_id]
    print(f"[retrieval] Final selected nodes: {titles}")

    return final_leaves


# ── Step 3: Fetch multiple node contents from Supabase ───────────────────────


async def _fetch_nodes_content(
    node_ids: list[str],
    document_id: str,
) -> list[dict]:
    """
    Fetch the full content + metadata for each node_id.
    Returns list of {node_id, title, path, level, page_start, page_end, content}.
    """
    if not node_ids:
        return []

    client = await _get_supabase()
    response = (
        await client.table("document_nodes")
        .select(
            "node_id, title, path, level, page_start, page_end, start_index, end_index, content"
        )
        .in_("node_id", node_ids)
        .eq("document_id", document_id)
        .execute()
    )

    if not response.data:
        return []

    # Preserve the order from node_ids (most relevant first)
    rows_by_id = {row["node_id"]: row for row in response.data}
    return [rows_by_id[nid] for nid in node_ids if nid in rows_by_id]


# ── Step 4: Synthesize answer from multi-node context ────────────────────────


async def _synthesize_answer(
    user_query: str,
    nodes: list[dict],
) -> str:
    """
    Ask Groq to synthesize a concise, voice-friendly answer from
    multiple retrieved node contents.

    This is the key PageIndex upgrade — instead of dumping raw section
    text, we let the LLM distill the answer from multi-node context.
    """
    if not nodes:
        return "I couldn't find relevant information for that question."

    if len(nodes) == 1:
        # Single node — still synthesize for voice-friendliness
        node = nodes[0]
        context = f"Section: {node['title']}\n\n{node['content']}"
    else:
        # Multi-node — combine with clear section headers
        parts = []
        for node in nodes:
            page_info = ""
            if node.get("page_start") is not None:
                page_info = f" (page {node['page_start'] + 1})"
            parts.append(
                f"--- {node['title']}{page_info} ---\n{node['content'][:1500]}"
            )
        context = "\n\n".join(parts)

    groq_client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])

    response = await groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a helpful voice assistant. Using ONLY the document sections provided, "
                    "answer the user's question in a concise, conversational way suitable for "
                    "text-to-speech (2-4 sentences max). Do not mention 'section' or 'document'. "
                    "If the sections don't contain the answer, say so briefly."
                ),
            },
            {
                "role": "user",
                "content": f"Question: {user_query}\n\nDocument content:\n{context}",
            },
        ],
        max_tokens=250,
        temperature=0.3,
    )

    return response.choices[0].message.content.strip()


# ── Public entry-point ────────────────────────────────────────────────────────


async def retrieve_knowledge(user_query: str, document_id: str) -> str:
    """
    Full PageIndex-aligned retrieval flow:
      Redis tree outline -> LLM Tree Search (node_list[]) -> Supabase multi-fetch
      -> Groq answer synthesis -> concise voice response

    Fallbacks:
      - If Redis outline missing: fetch outline directly from Supabase (re-caches it)
      - If tree search picks no nodes: use first 4 nodes ranked by content length
    """
    outline = await _fetch_outline(document_id)

    # ── Fallback: no Redis outline → load directly from Supabase ─────────────────
    if not outline:
        print(
            f"[retrieval] Redis miss for '{document_id}' — querying Supabase directly"
        )
        try:
            client = await _get_supabase()
            resp = (
                await client.table("document_nodes")
                .select(
                    "node_id, title, level, parent_id, path, summary, start_index, end_index, page_start, page_end"
                )
                .eq("document_id", document_id)
                .order("created_at")
                .execute()
            )
            outline = resp.data or []
            if outline:
                # Cache for next time
                redis = _get_redis()
                try:
                    await redis.set(
                        f"outline:{document_id}", json.dumps(outline), ex=3600
                    )
                finally:
                    await redis.aclose()
                print(
                    f"[retrieval] Loaded {len(outline)} nodes from Supabase, cached in Redis"
                )
        except Exception as e:
            print(f"[retrieval] Supabase fallback failed: {e}")

    if not outline:
        return (
            "The knowledge base for this document hasn't been loaded yet. "
            "Please upload and ingest a document first."
        )

    node_ids = await _tree_search(user_query, outline)

    # ── Fallback: tree search found nothing → take first 4 nodes by content ─────────
    if not node_ids:
        print(
            "[retrieval] Tree search returned empty — using first 4 nodes as fallback"
        )
        node_ids = [n["node_id"] for n in outline[:4]]

    nodes = await _fetch_nodes_content(node_ids, document_id)

    if not nodes:
        # Last resort: grab any 4 nodes directly
        try:
            client = await _get_supabase()
            resp = (
                await client.table("document_nodes")
                .select(
                    "node_id, title, path, level, page_start, page_end, start_index, end_index, content"
                )
                .eq("document_id", document_id)
                .limit(4)
                .execute()
            )
            nodes = resp.data or []
        except Exception as e:
            print(f"[retrieval] Last-resort fetch failed: {e}")

    if not nodes:
        return "I couldn't retrieve content from the knowledge base. Please try again."

    answer = await _synthesize_answer(user_query, nodes)
    return answer
