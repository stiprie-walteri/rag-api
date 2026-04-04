# Aviation MOE Compliance Checker + Persistent Document Store

## How to run locally!!!
Copy .env.example to .env

Ask Jēkabs or check Portainer for these variables:
```
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=... # This one can be taken from OpenRouter website, please select a free model
CLERK_SECRET_KEY=...
CLERK_FRONTEND_API_URL=...
```

Open docker desktop and run 
```
 docker compose up --build -d
```

To call some routes please run the trafficom-portal AKA frontend locally.

## What it does
- Upload and process MOE PDFs for legislation checks.
- Persist uploaded Markdown documents with organization scoping.
- Group every document inside a project within the user's workspace.
- Store document metadata/version/audit in PostgreSQL.
- Store Markdown bodies in MinIO using content-addressed object keys.

## New persistent document storage
- Bucket: `docstore` (configurable via `MINIO_BUCKET`).
- Object keys: `objects/<sha256>.md`.
- Versioning: each upload creates version `1` or increments version number.
- Dedupe: identical Markdown hashes point to the same MinIO object key.
- Audit: append-only rows for `document.upload` and `version.create` (plus optional read audits).
- Clerk auth model: each authenticated user gets a private internal organization/workspace automatically.

## Required environment variables
- `POSTGRES_DSN` (example: `postgresql://postgres:postgres@localhost:5432/rag_api`)
- `MINIO_ENDPOINT` (example: `localhost:9000`)
- `MINIO_ACCESS_KEY`
- `MINIO_SECRET_KEY`
- `MINIO_SECURE` (`true` or `false`, default `false`)
- `MINIO_BUCKET` (default `docstore`)
- `MIGRATIONS_DIR` (default `migrations`)
- `CLERK_FRONTEND_API_URL` (required for Clerk JWT verification)

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
- `GET /api/me`
- `GET /api/orgs/{organization_id}/projects`
- `POST /api/orgs/{organization_id}/projects`
- `GET /api/orgs/{organization_id}/projects/{project_id}`
- `PATCH /api/orgs/{organization_id}/projects/{project_id}`
- `DELETE /api/orgs/{organization_id}/projects/{project_id}`
- `GET /api/orgs/{organization_id}/projects/{project_id}/documents`
- `POST /api/documents/upload`
- `GET /api/orgs/{organization_id}/documents`
- `PATCH /api/orgs/{organization_id}/documents/{document_id}/project`
- `GET /api/orgs/{organization_id}/documents/{document_id}`
- `GET /api/orgs/{organization_id}/documents/{document_id}/versions/{version_no}`
- `GET /api/orgs/{organization_id}/documents/{document_id}/versions`
- `POST /api/admin/docstore/gc` (GC placeholder hook)

Each authenticated Clerk user gets exactly one private internal workspace. There is no cross-user document sharing in the current model.

## Upload request shape
`multipart/form-data`:
- `file` (`.md` / Markdown file, required)
- `organization_id` (UUID, optional; if omitted, the authenticated user's private workspace is resolved/created automatically. If provided, it must match that private workspace.)
- `project_id` (required for new documents, optional for new versions)
- `document_id` (UUID, optional; provide to create a new version)
- `title` (optional)
- `message` (optional)

Use `GET /api/me` first if the client needs the internal `organization_id` for `/orgs/{organization_id}/...` requests.

Upload response:
- `organization_id`
- `document_id`
- `version_id`
- `version_no`
- `content_hash`

## Migrations
- SQL migrations are in `migrations/`.
- On startup, the API applies unapplied SQL files tracked in `schema_migrations`.
- `002_clerk_user_access.sql` adds local user/org/document membership tables for Clerk-backed private workspaces.

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
- `CLERK_FRONTEND_API_URL=https://your-clerk-frontend-api`
