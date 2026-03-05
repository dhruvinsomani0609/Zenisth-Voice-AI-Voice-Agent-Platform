"""
scripts/run_ingestion.py
========================
CLI entry-point for the Vectorless RAG ingestion pipeline.

Runs the full pipeline:
  PDF → LlamaParse → Markdown → DocumentNode tree → Groq summaries
      → Supabase (upsert) → Redis (outline cache)

Usage:
    # From the project root:
    python scripts/run_ingestion.py --pdf zenisth_voice_agent_prd_trd.pdf --document-id zenisth_prd

    # With a custom Redis URL:
    python scripts/run_ingestion.py --pdf my_doc.pdf --document-id my_doc --redis-url redis://localhost:6379
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Allow imports from project root (e.g., app.services.*)
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.services.ingestion import ingest_pdf
from app.services.storage import cache_document_outline, insert_nodes


async def run(pdf_path: str, document_id: str) -> None:
    print("\n" + "=" * 60)
    print("  Zenisth Voice — Vectorless RAG Ingestion Pipeline")
    print("=" * 60)

    # ── Validate env vars ──────────────────────────────────────────────────────
    missing = [
        key
        for key in (
            "GROQ_API_KEY",
            "SUPABASE_URL",
            "SUPABASE_KEY",
        )
        if not os.environ.get(key)
    ]
    if missing:
        print(f"\n[ERROR] Missing environment variables: {', '.join(missing)}")
        print("        Please populate your .env file and retry.\n")
        sys.exit(1)

    # ── Validate PDF path ──────────────────────────────────────────────────────
    pdf_resolved = Path(pdf_path).resolve()
    if not pdf_resolved.exists():
        print(f"\n[ERROR] PDF not found: {pdf_resolved}\n")
        sys.exit(1)
    print(f"\n  PDF         : {pdf_resolved}")
    print(f"  Document ID : {document_id}\n")

    # ── Step 1-3: Ingest (parse + summarise) ──────────────────────────────────
    nodes = await ingest_pdf(str(pdf_resolved), document_id)
    print(f"\n[run_ingestion] Total nodes: {len(nodes)}")
    level_counts = {1: 0, 2: 0}
    for n in nodes:
        level_counts[n.level] = level_counts.get(n.level, 0) + 1
    print(f"  Level 1 nodes: {level_counts.get(1, 0)}")
    print(f"  Level 2 nodes: {level_counts.get(2, 0)}")

    # ── Step 4: Store in Supabase ──────────────────────────────────────────────
    print("\n[run_ingestion] Inserting nodes into Supabase ...")
    await insert_nodes(nodes)
    print("[run_ingestion] ✓ Supabase insert complete")

    # ── Step 5: Cache outline in Redis ────────────────────────────────────────
    print("\n[run_ingestion] Caching document outline in Redis ...")
    await cache_document_outline(document_id)
    print("[run_ingestion] ✓ Redis cache complete")

    print("\n" + "=" * 60)
    print("  Ingestion complete! You can now run the retrieval engine.")
    print(f"  Test with:")
    print(f'    python -c "')
    print(f"    import asyncio")
    print(f"    from app.services.retrieval import retrieve_knowledge")
    print(
        f"    print(asyncio.run(retrieve_knowledge('your question here', '{document_id}')))"
    )
    print(f'    "')
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Zenisth Voice — Vectorless RAG ingestion pipeline"
    )
    parser.add_argument(
        "--pdf",
        required=True,
        help="Path to the PDF file to ingest (e.g. zenisth_voice_agent_prd_trd.pdf)",
    )
    parser.add_argument(
        "--document-id",
        required=True,
        help="Unique identifier for this document (used as lookup key in Redis + Supabase)",
    )
    parser.add_argument(
        "--redis-url",
        default=None,
        help="Redis connection URL (default: redis://localhost:6379, or REDIS_URL env var)",
    )
    args = parser.parse_args()

    if args.redis_url:
        os.environ["REDIS_URL"] = args.redis_url

    asyncio.run(run(args.pdf, args.document_id))


if __name__ == "__main__":
    main()
