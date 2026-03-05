-- Migration to change UUID columns to TEXT to allow non-UUID identifiers for documents and organizations.

-- 1. Drop constraints that might block type changes
ALTER TABLE organization_memberships DROP CONSTRAINT IF EXISTS organization_memberships_organization_id_fkey;
ALTER TABLE document_memberships DROP CONSTRAINT IF EXISTS fk_document_memberships_document;
ALTER TABLE documents DROP CONSTRAINT IF EXISTS fk_documents_current_version;
ALTER TABLE document_versions DROP CONSTRAINT IF EXISTS fk_document_versions_document;
ALTER TABLE document_versions DROP CONSTRAINT IF EXISTS document_versions_parent_version_id_fkey;
ALTER TABLE document_versions DROP CONSTRAINT IF EXISTS document_versions_organization_id_fkey;
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_organization_id_fkey;
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_organization_id_fkey;

-- 2. Change column types to TEXT
ALTER TABLE organizations ALTER COLUMN id TYPE TEXT USING id::TEXT;

ALTER TABLE documents ALTER COLUMN id TYPE TEXT USING id::TEXT;
ALTER TABLE documents ALTER COLUMN organization_id TYPE TEXT USING organization_id::TEXT;
ALTER TABLE documents ALTER COLUMN current_version_id TYPE TEXT USING current_version_id::TEXT;

ALTER TABLE document_versions ALTER COLUMN id TYPE TEXT USING id::TEXT;
ALTER TABLE document_versions ALTER COLUMN organization_id TYPE TEXT USING organization_id::TEXT;
ALTER TABLE document_versions ALTER COLUMN document_id TYPE TEXT USING document_id::TEXT;
ALTER TABLE document_versions ALTER COLUMN parent_version_id TYPE TEXT USING parent_version_id::TEXT;

ALTER TABLE audit_log ALTER COLUMN organization_id TYPE TEXT USING organization_id::TEXT;
ALTER TABLE audit_log ALTER COLUMN object_id TYPE TEXT USING object_id::TEXT;

ALTER TABLE organization_memberships ALTER COLUMN organization_id TYPE TEXT USING organization_id::TEXT;

ALTER TABLE document_memberships ALTER COLUMN organization_id TYPE TEXT USING organization_id::TEXT;
ALTER TABLE document_memberships ALTER COLUMN document_id TYPE TEXT USING document_id::TEXT;

-- 3. Restore constraints
ALTER TABLE documents
    ADD CONSTRAINT documents_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE document_versions
    ADD CONSTRAINT document_versions_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE document_versions
    ADD CONSTRAINT document_versions_parent_version_id_fkey
    FOREIGN KEY (parent_version_id) REFERENCES document_versions(id);

ALTER TABLE document_versions
    ADD CONSTRAINT fk_document_versions_document
    FOREIGN KEY (organization_id, document_id) REFERENCES documents(organization_id, id) ON DELETE CASCADE;

ALTER TABLE documents
    ADD CONSTRAINT fk_documents_current_version
    FOREIGN KEY (current_version_id) REFERENCES document_versions(id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE audit_log
    ADD CONSTRAINT audit_log_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE organization_memberships
    ADD CONSTRAINT organization_memberships_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE document_memberships
    ADD CONSTRAINT fk_document_memberships_document
    FOREIGN KEY (organization_id, document_id) REFERENCES documents(organization_id, id) ON DELETE CASCADE;
