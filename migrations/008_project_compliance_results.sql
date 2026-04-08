ALTER TABLE folders
    ADD COLUMN IF NOT EXISTS compliance_result JSONB NULL;
