-- Zenisth Voice — Document Nodes Schema (PageIndex-aligned)
-- Run this in Supabase SQL editor
--
-- IMPORTANT: Drop and recreate the table to fix the node_id type.
-- The node_id must be TEXT (not uuid) because our code generates UUID strings.

-- Drop the old table (this removes all data!)
DROP TABLE IF EXISTS document_nodes CASCADE;

-- Recreate with correct schema
CREATE TABLE document_nodes (
    node_id       TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    level         INTEGER NOT NULL DEFAULT 1,
    parent_id     TEXT,
    path          TEXT DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL DEFAULT '',
    start_index   INTEGER DEFAULT 0,
    end_index     INTEGER DEFAULT 0,
    page_start    INTEGER DEFAULT 0,
    page_end      INTEGER DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for fast queries
CREATE INDEX IF NOT EXISTS idx_docnodes_docid
    ON document_nodes(document_id);

CREATE INDEX IF NOT EXISTS idx_docnodes_parent
    ON document_nodes(parent_id);

CREATE INDEX IF NOT EXISTS idx_docnodes_level
    ON document_nodes(document_id, level);

CREATE INDEX IF NOT EXISTS idx_docnodes_pages
    ON document_nodes(document_id, page_start, page_end);
