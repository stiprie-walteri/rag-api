# Aviation MOE Compliance Checker + Persistent Document Store

## What it does
- Upload and process MOE PDFs for legislation checks.
- Persist uploaded Markdown documents with organization scoping.
- Store document metadata/version/audit in PostgreSQL.
- Store Markdown bodies in MinIO using content-addressed object keys.

## New persistent document storage
- Bucket: `docstore` (configurable via `MINIO_BUCKET`).
- Object keys: `objects/<sha256>.md`.
- Versioning: each upload creates version `1` or increments version number.
- Dedupe: identical Markdown hashes point to the same MinIO object key.
- Audit: append-only rows for `document.upload` and `version.create` (plus optional read audits).

## Required environment variables
- `POSTGRES_DSN` (example: `postgresql://postgres:postgres@localhost:5432/rag_api`)
- `MINIO_ENDPOINT` (example: `localhost:9000`)
- `MINIO_ACCESS_KEY`
- `MINIO_SECRET_KEY`
- `MINIO_SECURE` (`true` or `false`, default `false`)
- `MINIO_BUCKET` (default `docstore`)
- `MIGRATIONS_DIR` (default `migrations`)

## Storage containers (independent from API)
The API is not part of the storage compose stack. You can run data services separately.

1. Copy storage env file:
```bash
Copy-Item .env.storage.example .env.storage   # PowerShell
cp .env.storage.example .env.storage
```
2. Start both storages:
```bash
docker compose --env-file .env.storage -f docker-compose.storage.yml up -d postgres minio minio-init
```
3. Start only one service if needed:
```bash
docker compose --env-file .env.storage -f docker-compose.storage.yml up -d postgres
docker compose --env-file .env.storage -f docker-compose.storage.yml up -d minio minio-init
```
4. Stop storages:
```bash
docker compose --env-file .env.storage -f docker-compose.storage.yml down
```

Default endpoints from this compose stack:
- Postgres: `localhost:5432`
- MinIO API: `localhost:9000`
- MinIO console: `http://localhost:9001`

## API endpoints
- `POST /documents/upload` (also available as `/api/documents/upload`)
- `GET /orgs/{organization_id}/documents` (also available as `/api/orgs/{organization_id}/documents`)
- `GET /orgs/{organization_id}/documents/{document_id}` (also available as `/api/...`)
- `GET /orgs/{organization_id}/documents/{document_id}/versions/{version_no}` (also available as `/api/...`)
- `GET /orgs/{organization_id}/documents/{document_id}/versions` (also available as `/api/...`)
- `POST /api/admin/docstore/gc` (GC placeholder hook)

## Upload request shape
`multipart/form-data`:
- `organization_id` (UUID, required)
- `actor_user_id` (required)
- `file` (`.md` / Markdown file, required)
- `document_id` (UUID, optional; provide to create a new version)
- `title` (optional)
- `message` (optional)

Upload response:
- `document_id`
- `version_id`
- `version_no`
- `content_hash`

## Migrations
- SQL migrations are in `migrations/`.
- On startup, the API applies unapplied SQL files tracked in `schema_migrations`.

## Run
```bash
pip install -r requirements.txt
python main.py
```

Use these API env values with the storage compose defaults:
- `POSTGRES_DSN=postgresql://rag_user:rag_password@localhost:5432/rag_api`
- `MINIO_ENDPOINT=localhost:9000`
- `MINIO_ACCESS_KEY=minioadmin`
- `MINIO_SECRET_KEY=minioadmin`
- `MINIO_SECURE=false`
- `MINIO_BUCKET=docstore`
