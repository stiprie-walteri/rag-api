ALTER TABLE folders
    ADD COLUMN IF NOT EXISTS legislation_template_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

UPDATE folders
SET legislation_template_ids = '[]'::jsonb
WHERE legislation_template_ids IS NULL;
