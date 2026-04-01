ALTER TABLE folders
    ADD COLUMN IF NOT EXISTS description TEXT NULL;

ALTER TABLE folders
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NULL;

ALTER TABLE folders
    ADD COLUMN IF NOT EXISTS updated_by TEXT NULL;

UPDATE folders
SET updated_at = COALESCE(updated_at, created_at),
    updated_by = COALESCE(updated_by, created_by, 'system')
WHERE updated_at IS NULL
   OR updated_by IS NULL;

ALTER TABLE folders
    ALTER COLUMN updated_at SET DEFAULT NOW();

ALTER TABLE folders
    ALTER COLUMN updated_at SET NOT NULL;

ALTER TABLE folders
    ALTER COLUMN updated_by SET DEFAULT 'system';

ALTER TABLE folders
    ALTER COLUMN updated_by SET NOT NULL;

WITH organizations_missing_projects AS (
    SELECT
        d.organization_id,
        COALESCE(NULLIF(MIN(d.created_by), ''), 'system') AS actor_user_id
    FROM documents d
    WHERE d.folder_id IS NULL
    GROUP BY d.organization_id
),
inserted_projects AS (
    INSERT INTO folders (
        organization_id,
        name,
        description,
        created_at,
        created_by,
        updated_at,
        updated_by
    )
    SELECT
        organization_id,
        'General',
        'Auto-created during project migration for documents not previously assigned to a project.',
        NOW(),
        actor_user_id,
        NOW(),
        actor_user_id
    FROM organizations_missing_projects
    RETURNING id, organization_id
)
UPDATE documents d
SET folder_id = p.id
FROM inserted_projects p
WHERE d.organization_id = p.organization_id
  AND d.folder_id IS NULL;

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_folder_id_fkey;

ALTER TABLE documents
    ADD CONSTRAINT documents_folder_id_fkey
    FOREIGN KEY (folder_id)
    REFERENCES folders(id)
    ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_documents_organization_id_folder_id_updated_at_desc
    ON documents (organization_id, folder_id, updated_at DESC);

ALTER TABLE documents
    ALTER COLUMN folder_id SET NOT NULL;
