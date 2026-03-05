"""
app/services/ingestion.py
=========================
Vectorless RAG — Ingestion Pipeline  (PageIndex-aligned)

Steps:
  1. Convert PDF/DOCX/TXT → Markdown via native parsers:
       • PDF  → PyMuPDF (fitz): font-size analysis detects heading levels
       • DOCX → python-docx:   reads native Heading 1/2/3 paragraph styles
       • TXT  → plain text, fall-through to headerless chunker
  2. Split Markdown by headers (#, ##, ###, ####) → true multi-level tree
     Each node tracks: level, parent_id, page_start, page_end
  3. Generate a 1-sentence summary per node via Groq (async, rate-limited)

Tree structure mirrors PageIndex:
  {node_id, title, level, parent_id, path, summary, content, page_start, page_end}

Supported file types: .pdf, .docx, .txt
Note: Image-only (scanned) PDFs will produce empty text — a clear error is raised.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

# Ensure stdout handles arbitrary unicode on Windows (cp1252 console)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

from groq import AsyncGroq
from pydantic import BaseModel


# ── Pydantic model ─────────────────────────────────────────────────────────────


class DocumentNode(BaseModel):
    title: str
    node_id: str
    start_index: int = 0
    end_index: int = 0
    summary: str = ""
    nodes: list[DocumentNode] = []

    # Internal tracking fields (not necessarily serialized to JSON in final out if we dump strictly)
    document_id: str = ""
    level: int = 1
    parent_id: Optional[str] = None
    path: str = ""
    content: str = ""
    page_start: int = 0
    page_end: int = 0

    def dump_pageindex_format(self) -> dict:
        """Serialize exactly to the PageIndex tree structure requested by the user."""
        return {
            "title": self.title,
            "node_id": self.node_id,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "summary": self.summary,
            "nodes": [child.dump_pageindex_format() for child in self.nodes],
        }


# ── Supported extensions ───────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt"}


# ── Step 1: File → Markdown (native parsers, no ML required) ─────────────────


def _pdf_to_markdown(file_path: str) -> str:
    """
    Convert a PDF to Markdown using PyMuPDF.
    Strategy:
      - Collect all unique font sizes across the document.
      - The largest font sizes are treated as headings (levels 1-3).
      - Each text block is emitted as `# Title`, `## Sub`, `### Sub-sub`, or plain body.
      - Page boundaries are tracked for page_start / page_end metadata.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(file_path)
    if doc.page_count == 0:
        raise ValueError("PDF has no pages.")

    # ── Collect all font sizes used in the document ────────────────────────────
    all_sizes: set[float] = set()
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    sz = round(span.get("size", 0), 1)
                    if sz > 0:
                        all_sizes.add(sz)

    if not all_sizes:
        raise ValueError("Image-only PDF — no text layer found.")

    # Sort sizes descending; top 3 distinct sizes are heading levels 1, 2, 3
    sorted_sizes = sorted(all_sizes, reverse=True)
    # Deduplicate within a 1pt tolerance
    heading_sizes: list[float] = []
    for sz in sorted_sizes:
        if not heading_sizes or (heading_sizes[-1] - sz) > 1.0:
            heading_sizes.append(sz)
        if len(heading_sizes) == 3:
            break

    def _heading_level(size: float) -> int:
        """Return 1, 2, or 3 if the size matches a heading tier, else 0 (body)."""
        for i, hs in enumerate(heading_sizes):
            if abs(size - hs) <= 1.0:
                return i + 1
        return 0

    lines_md: list[str] = []
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:  # skip image blocks
                continue
            for line in block.get("lines", []):
                parts: list[str] = []
                max_size = 0.0
                is_bold = False
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue
                    sz = round(span.get("size", 0), 1)
                    flags = span.get("flags", 0)
                    if sz > max_size:
                        max_size = sz
                    if flags & 2**4:  # bold flag in PyMuPDF
                        is_bold = True
                    parts.append(text)
                if not parts:
                    continue
                text = " ".join(parts)
                level = _heading_level(max_size)
                # Treat first-tier bold lines as headings even if size barely misses
                if (
                    level == 0
                    and is_bold
                    and max_size >= (heading_sizes[-1] if heading_sizes else 0)
                ):
                    level = len(heading_sizes)
                if level > 0:
                    lines_md.append(f"{'#' * level} {text}")
                else:
                    lines_md.append(text)

    doc.close()
    markdown = "\n".join(lines_md)
    if not markdown.strip():
        raise ValueError("Image-only PDF — no text layer found.")
    return markdown


def _docx_to_markdown(file_path: str) -> str:
    """
    Convert a DOCX to Markdown using python-docx.
    Reads native paragraph styles: 'Heading 1' → #, 'Heading 2' → ##, etc.
    Falls back to bold-detection for documents without proper style names.
    """
    import docx  # python-docx

    document = docx.Document(file_path)
    lines_md: list[str] = []

    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        style_name = para.style.name if para.style else ""
        # Native heading styles: 'Heading 1', 'Heading 2', ..., 'Heading 9'
        if style_name.startswith("Heading "):
            try:
                level = int(style_name.split()[-1])
                level = min(level, 4)  # cap at ####
            except ValueError:
                level = 1
            lines_md.append(f"{'#' * level} {text}")
        else:
            # Fallback: if entire paragraph is bold, treat as heading
            all_bold = all(r.bold for r in para.runs if r.text.strip())
            if all_bold and para.runs and len(text.split()) <= 10:
                lines_md.append(f"## {text}")
            else:
                lines_md.append(text)

    markdown = "\n".join(lines_md)
    if not markdown.strip():
        raise ValueError("DOCX has no readable text content.")
    return markdown


async def parse_file_to_markdown(file_path: str) -> str:
    """
    Convert a PDF, DOCX, or TXT file to hierarchical Markdown.

    • PDF  → PyMuPDF font-size analysis (heading levels via font size tiers)
    • DOCX → python-docx native Heading styles (Heading 1 → #, Heading 2 → ##)
    • TXT  → read as-is; downstream headerless splitter handles chunking

    No ML models or external APIs required.

    Raises:
        ValueError: if the file type is unsupported or the document has no text.
    """
    ext = Path(file_path).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{ext}'. Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
        )

    loop = asyncio.get_event_loop()

    if ext == ".pdf":

        def _convert() -> str:
            return _pdf_to_markdown(file_path)

    elif ext == ".docx":

        def _convert() -> str:
            return _docx_to_markdown(file_path)

    else:  # .txt

        def _convert() -> str:
            return Path(file_path).read_text(encoding="utf-8", errors="replace")

    markdown = await loop.run_in_executor(None, _convert)

    if not markdown or not markdown.strip():
        raise ValueError(
            "No readable text found. "
            "Please upload a text-searchable file (not a scanned image PDF)."
        )

    print(f"[ingestion] Extracted {len(markdown)} chars from '{Path(file_path).name}'")
    return markdown


# ── Step 2: Markdown → Multi-level DocumentNode tree ──────────────────────────

# Header pattern: level = number of leading # chars (1-4)
_HEADER_RE = re.compile(r"^(#{1,4}) (.+)$")


def _split_headerless(markdown: str) -> list[dict]:
    """
    Fallback splitter for documents with no Markdown headers.

    Title-detection rules (must ALL pass to be treated as a section heading):
      - 2–8 words, 4–70 chars
      - Does NOT end with . , ; : ( ) = or code-style chars
      - Does NOT contain code patterns: (, ), =, ->, :=, def , class , import ,
        logger, logging, async, await, self., __
      - Is either ALL CAPS, or matches loose Title-Case/sentence-start pattern
      - Not a page break marker (handled separately)

    Fall-through: if fewer than 3 real sections found, switch to fixed-size chunking.
    """
    # Normalize page breaks
    text = markdown.replace("\x0c", "\n\n--- PAGE BREAK ---\n\n")
    lines = text.splitlines()

    # Patterns that definitively indicate a CODE line (never a section title)
    _CODE_PATTERNS = re.compile(
        r"[()\[\]=<>{}]|->|:=|^\s*(def |class |import |from |logger|logging|async def"
        r"|self\.|__|\#|await |\@)|\bgetLogger\b|\bsetLevel\b|\baddHandler\b"
    )
    _TITLE_RE = re.compile(r"^[A-Z][A-Za-z0-9 &,\-:/]{3,65}$")

    def _is_title(line: str) -> bool:
        s = line.strip()
        if not s or len(s) < 4 or len(s) > 70:
            return False
        # Hard filters — code artifacts
        if _CODE_PATTERNS.search(s):
            return False
        words = s.split()
        if len(words) < 2 or len(words) > 8:
            return False
        if s[-1] in ".,;:()=\\|/`\"'":
            return False
        # Page-break markers → handled as page separator
        if "PAGE BREAK" in s:
            return True
        # ALL CAPS → strong heading signal
        if s.isupper():
            return True
        # Title-case or sentence-start match
        if _TITLE_RE.match(s):
            return True
        return False

    raw: list[dict] = []
    current_title: str | None = None
    current_body: list[str] = []
    page_num = 0

    def _flush():
        nonlocal current_title, current_body
        body = "\n".join(current_body).strip()
        if body and current_title:
            raw.append({"title": current_title, "level": 1, "content": body})
        current_body = []

    for line in lines:
        if _is_title(line.strip()):
            _flush()
            if "PAGE BREAK" in line:
                page_num += 1
                current_title = f"Page {page_num}"
            else:
                current_title = line.strip()
        else:
            if current_title is None and line.strip():
                current_title = "Introduction"
            current_body.append(line)

    _flush()

    # ── If we got fewer than 3 meaningful sections, use fixed-size chunking instead ──
    # This handles flat/code-heavy documents that trick the title detector
    if len(raw) < 3:
        raw = []
        full = markdown.strip()
        chunk_size = 1200
        for i, start in enumerate(range(0, len(full), chunk_size)):
            chunk = full[start : start + chunk_size].strip()
            if chunk:
                raw.append({"title": f"Section {i + 1}", "level": 1, "content": chunk})

    return raw or [
        {"title": "Document Content", "level": 1, "content": markdown.strip()}
    ]


def _split_by_headers(
    markdown: str,
    document_id: str,
) -> list[DocumentNode]:
    """
    Splits Markdown into DocumentNode objects supporting 4 header levels.
    Tracks exact character offsets (start_index, end_index) and builds an in-memory tree.
    """
    parent_stack: list[Optional[str]] = [None, None, None, None]
    path_stack: list[str] = ["", "", "", ""]

    # Track logical blocks with their start/end offsets
    # A block is [title, level, content, start_index, end_index]
    raw_sections: list[dict] = []

    current_title = "__root__"
    current_level = 1
    current_lines: list[str] = []
    current_start = 0

    # We iterate through character indices to find lines so we can track exact offsets
    lines_with_offsets = []
    idx = 0
    for line in markdown.splitlines(keepends=True):
        lines_with_offsets.append((line, idx))
        idx += len(line)

    def _flush(end_idx: int):
        nonlocal current_lines, current_title, current_level, current_start
        content = "".join(current_lines).strip()
        if current_title != "__root__" and content:
            raw_sections.append(
                {
                    "title": current_title,
                    "level": current_level,
                    "content": content,
                    "start_index": current_start,
                    "end_index": end_idx,
                }
            )

    for line, char_idx in lines_with_offsets:
        clean_line = line.rstrip("\r\n")
        m = _HEADER_RE.match(clean_line)
        if m:
            _flush(end_idx=char_idx)
            current_title = m.group(2).strip()
            current_level = len(m.group(1))
            current_lines = [line]
            current_start = char_idx
        else:
            current_lines.append(line)

    _flush(end_idx=len(markdown))

    if not raw_sections:
        # Fallback to headerless logic
        return _build_nodes_from_raw(_split_headerless(markdown), document_id)

    return _build_nodes_from_raw(raw_sections, document_id)


def _build_nodes_from_raw(
    raw_sections: list[dict], document_id: str
) -> list[DocumentNode]:
    nodes: list[DocumentNode] = []
    parent_stack: list[Optional[str]] = [None, None, None, None]
    path_stack: list[str] = ["", "", "", ""]

    for raw in raw_sections:
        level = raw.get("level", 1)
        node_id = str(uuid.uuid4())
        # Walk up from (level-2) to 0 to find the nearest available ancestor.
        # This handles documents that skip heading levels (e.g. Level 1 → Level 3).
        parent_id: Optional[str] = None
        if level >= 2:
            for ancestor_level in range(level - 2, -1, -1):
                if parent_stack[ancestor_level] is not None:
                    parent_id = parent_stack[ancestor_level]
                    break

        path_stack[level - 1] = raw["title"]
        for deeper in range(level, 4):
            path_stack[deeper] = ""
        path = " > ".join(p for p in path_stack if p)

        parent_stack[level - 1] = node_id
        for deeper in range(level, 4):
            parent_stack[deeper] = None

        nodes.append(
            DocumentNode(
                node_id=node_id,
                document_id=document_id,
                title=raw["title"],
                level=level,
                parent_id=parent_id,
                path=path,
                start_index=raw.get("start_index", 0),
                end_index=raw.get("end_index", len(raw["content"])),
                content=raw["content"],
                summary="",
            )
        )
    return nodes


# ── Step 3: Generate summaries via Groq ────────────────────────────────────────


async def _summarise_node(
    client: AsyncGroq, node: DocumentNode, children: list[DocumentNode]
) -> DocumentNode:
    """
    Groq llama-3.1-8b-instant → 1-sentence summary for tree indexing.
    Includes the path breadcrumb and the summaries of any child sections
    so parent nodes accurately roll up the semantic meaning of their branch.
    """
    if len(node.content) < 200 and not children:
        return node.model_copy(update={"summary": node.content.strip()})

    child_context = ""
    if children:
        child_context = "\n\nThis section contains the following sub-sections:\n"
        for child in children:
            child_context += f"- {child.title}: {child.summary}\n"

    prompt = (
        f"You are a document indexer building a tree table-of-contents for fast retrieval.\n"
        f"Section path: {node.path}\n"
        f"Section title: {node.title}\n"
        f"Content (first 1000 chars):\n{node.content[:1000]}\n"
        f"{child_context}\n"
        f"Write exactly ONE concise sentence (≤30 words) capturing what this section (and its sub-sections) covers.\n"
        f"Summary sentence:"
    )

    try:
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.1,
        )
        summary = response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ingestion] Groq summarization failed for {node.node_id}: {e}")
        summary = node.content[:100].strip() + "..."

    return node.model_copy(update={"summary": summary})


async def _summarise_all(nodes: list[DocumentNode]) -> list[DocumentNode]:
    """
    Summarise nodes bottom-up (leaves first, then parents).
    This ensures parents can include their children's summaries in their own prompt.
    Concurrent within each level.
    """
    client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
    semaphore = asyncio.Semaphore(5)

    # 1. Group nodes by level
    by_id = {n.node_id: n for n in nodes}
    children_map: dict[str, list[str]] = {n.node_id: [] for n in nodes}
    levels: dict[int, list[str]] = {}

    for n in nodes:
        if n.parent_id and n.parent_id in children_map:
            children_map[n.parent_id].append(n.node_id)

        if n.level not in levels:
            levels[n.level] = []
        levels[n.level].append(n.node_id)

    if not levels:
        return nodes

    max_level = max(levels.keys())

    async def _bounded(nid: str) -> DocumentNode:
        async with semaphore:
            node = by_id[nid]
            child_nodes = [by_id[cid] for cid in children_map[nid]]
            new_node = await _summarise_node(client, node, child_nodes)
            by_id[nid] = new_node
            return new_node

    # 2. Process bottom-up (level N down to level 1)
    for lvl in range(max_level, 0, -1):
        if lvl not in levels:
            continue
        print(f"[ingestion] Summarizing Level {lvl} ({len(levels[lvl])} nodes)...")
        tasks = [_bounded(nid) for nid in levels[lvl]]
        await asyncio.gather(*tasks)

    # 3. Assemble the nested tree structure
    roots = []
    for node in nodes:
        # Get the latest version from by_id list which has the summaries
        hydrated_node = by_id[node.node_id]

        # Hydrate children
        child_ids = children_map.get(hydrated_node.node_id, [])
        hydrated_node.nodes = [by_id[cid] for cid in child_ids]

        if not hydrated_node.parent_id:
            roots.append(hydrated_node)

    # If everything is flat (no parents), treat all as roots
    if not roots:
        roots = [by_id[n.node_id] for n in nodes if n.level == 1]
    if not roots:
        roots = [by_id[n.node_id] for n in nodes]

    return roots


# ── Public entry-point ─────────────────────────────────────────────────────────


async def ingest_document(file_path: str, document_id: str) -> list[DocumentNode]:
    """
    Full ingestion pipeline (PageIndex-aligned):
      File → Markdown (MarkItDown) → Multi-level DocumentNode tree (with summaries)

    Supports: .pdf, .docx, .txt

    Args:
        file_path:   Absolute or relative path to the file.
        document_id: Unique identifier for this document.

    Returns:
        List of DocumentNode objects with level, parent_id, path,
        page_start, page_end, and summary — ready for storage.
    """
    print(f"[ingestion] Parsing file: {file_path}")
    markdown = await parse_file_to_markdown(file_path)
    print(f"[ingestion] Markdown: {len(markdown)} chars")

    nodes = _split_by_headers(markdown, document_id)
    if nodes:
        max_level = max(n.level for n in nodes)
        print(f"[ingestion] Extracted {len(nodes)} nodes across {max_level} levels")
    else:
        raise ValueError("No content sections found in the document.")

    print("[ingestion] Generating Groq summaries...")
    root_nodes = await _summarise_all(nodes)
    print("[ingestion] Summaries complete")

    # Debug: Print the exact PageIndex JSON format of the first root node
    if root_nodes:
        print("\n\n[ingestion] --- PAGEINDEX JSON TREE PREVIEW ---")
        preview = root_nodes[0].dump_pageindex_format()
        print(json.dumps(preview, indent=2))
        print("[ingestion] ---------------------------------------\n\n")

    return root_nodes


# ── Backward-compatible alias ──────────────────────────────────────────────────
async def ingest_pdf(pdf_path: str, document_id: str) -> list[DocumentNode]:
    """Backward-compatible alias for ingest_document."""
    return await ingest_document(pdf_path, document_id)
