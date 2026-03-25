ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS compliance_result JSONB NULL;
