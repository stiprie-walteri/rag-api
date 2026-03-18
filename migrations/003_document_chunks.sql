-- Migration to add a document chunks table for storing section-based text chunks from PDFs

CREATE TABLE IF NOT EXISTS document_chunks (
    id UUID PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    chunk_level INT NOT NULL,
    title TEXT NULL,
    start_page INT NOT NULL,
    end_page INT NOT NULL,
    text_content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_document_chunks_document
        FOREIGN KEY (organization_id, document_id)
        REFERENCES documents(organization_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_document_version
    ON document_chunks (organization_id, document_id, version_id);

CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id
    ON document_chunks (document_id);
