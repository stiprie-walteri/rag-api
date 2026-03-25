CREATE TABLE IF NOT EXISTS folders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_folders_organization_id ON folders (organization_id);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS folder_id UUID NULL REFERENCES folders(id) ON DELETE SET NULL;
