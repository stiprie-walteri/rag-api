CREATE TABLE IF NOT EXISTS organizations (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    title TEXT NULL,
    current_version_id UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_organization_id_id
    ON documents (organization_id, id);

CREATE INDEX IF NOT EXISTS idx_documents_organization_id_updated_at_desc
    ON documents (organization_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS document_versions (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id UUID NOT NULL,
    version_no INT NOT NULL CHECK (version_no > 0),
    content_hash TEXT NOT NULL,
    object_key TEXT NOT NULL,
    size_bytes INT NOT NULL CHECK (size_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT NOT NULL,
    message TEXT NULL,
    parent_version_id UUID NULL REFERENCES document_versions(id),
    CONSTRAINT fk_document_versions_document
        FOREIGN KEY (organization_id, document_id)
        REFERENCES documents(organization_id, id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_versions_organization_id_document_id_version_no
    ON document_versions (organization_id, document_id, version_no);

CREATE INDEX IF NOT EXISTS idx_document_versions_document_id
    ON document_versions (document_id);

CREATE INDEX IF NOT EXISTS idx_document_versions_organization_id
    ON document_versions (organization_id);

CREATE INDEX IF NOT EXISTS idx_document_versions_organization_id_document_id_version_no_desc
    ON document_versions (organization_id, document_id, version_no DESC);

ALTER TABLE documents
ADD CONSTRAINT fk_documents_current_version
FOREIGN KEY (current_version_id)
REFERENCES document_versions(id)
DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id UUID NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata_json JSONB NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_organization_id_at_desc
    ON audit_log (organization_id, at DESC);
