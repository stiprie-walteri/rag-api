ALTER TABLE organizations
ADD COLUMN IF NOT EXISTS clerk_org_id TEXT NULL;

ALTER TABLE organizations
ADD COLUMN IF NOT EXISTS clerk_org_slug TEXT NULL;

ALTER TABLE organizations
ADD COLUMN IF NOT EXISTS name TEXT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_organizations_clerk_org_id
    ON organizations (clerk_org_id)
    WHERE clerk_org_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    primary_email TEXT NULL,
    first_name TEXT NULL,
    last_name TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS organization_memberships (
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT NOT NULL,
    PRIMARY KEY (organization_id, user_id),
    CONSTRAINT chk_organization_memberships_role
        CHECK (role IN ('owner', 'member'))
);

CREATE INDEX IF NOT EXISTS idx_organization_memberships_user_id
    ON organization_memberships (user_id);

CREATE TABLE IF NOT EXISTS document_memberships (
    organization_id UUID NOT NULL,
    document_id UUID NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    assigned_by TEXT NOT NULL,
    PRIMARY KEY (organization_id, document_id, user_id),
    CONSTRAINT fk_document_memberships_document
        FOREIGN KEY (organization_id, document_id)
        REFERENCES documents(organization_id, id)
        ON DELETE CASCADE,
    CONSTRAINT chk_document_memberships_role
        CHECK (role IN ('owner', 'editor', 'viewer'))
);

CREATE INDEX IF NOT EXISTS idx_document_memberships_user_lookup
    ON document_memberships (organization_id, user_id, assigned_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_memberships_document_lookup
    ON document_memberships (organization_id, document_id);

INSERT INTO users (id, created_at, updated_at, last_seen_at)
SELECT DISTINCT created_by, NOW(), NOW(), NOW()
FROM documents
WHERE created_by IS NOT NULL
  AND created_by <> ''
ON CONFLICT (id) DO NOTHING;

INSERT INTO organization_memberships (
    organization_id,
    user_id,
    role,
    created_at,
    created_by,
    updated_at,
    updated_by
)
SELECT DISTINCT
    d.organization_id,
    d.created_by,
    'owner',
    NOW(),
    d.created_by,
    NOW(),
    d.created_by
FROM documents d
WHERE d.created_by IS NOT NULL
  AND d.created_by <> ''
ON CONFLICT (organization_id, user_id) DO NOTHING;

INSERT INTO document_memberships (
    organization_id,
    document_id,
    user_id,
    role,
    assigned_at,
    assigned_by
)
SELECT
    d.organization_id,
    d.id,
    d.created_by,
    'owner',
    NOW(),
    d.created_by
FROM documents d
WHERE d.created_by IS NOT NULL
  AND d.created_by <> ''
ON CONFLICT (organization_id, document_id, user_id) DO NOTHING;
