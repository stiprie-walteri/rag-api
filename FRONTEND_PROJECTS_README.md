# Frontend Task: Project-Based Document UI

## Goal

Update the frontend so documents are managed inside projects instead of folders.

Every document now belongs to exactly one project. The backend migrates old data automatically:

- old folders become projects
- documents that were not in a folder are moved into a generated `General` project

The frontend should remove folder-based flows and replace them with project-based flows.

## Authentication

All requests still require the Clerk bearer token:

```http
Authorization: Bearer <clerk-jwt>
```

Resolve the internal organization/workspace first:

```http
GET /api/me
```

Use the returned `organization_id` for all project and document endpoints.

## Required UI Work

1. Add a project list view for the current workspace.
2. Add create project UI.
3. Add edit project UI for name and description.
4. Add delete project UI with confirmation.
5. Add a project detail view that lists the documents inside that project.
6. Require project selection when uploading a new document.
7. Add a move-document action so an existing document can be reassigned to a different project.
8. Show the current project on document cards/tables/detail views.
9. Remove or hide any folder-specific UI and API usage.

## Backend Endpoints

### 1. Get current workspace

```http
GET /api/me
```

Use `organization_id` from the response.

### 2. List projects

```http
GET /api/orgs/{organization_id}/projects
```

Response:

```json
[
  {
    "project_id": "550e8400-e29b-41d4-a716-446655440000",
    "organization_id": "org_123",
    "name": "General",
    "description": "Auto-created during project migration for documents that were not in a folder.",
    "created_at": "2026-04-01T12:00:00Z",
    "created_by": "user_123",
    "updated_at": "2026-04-01T12:00:00Z",
    "updated_by": "user_123",
    "document_count": 4
  }
]
```

### 3. Create project

```http
POST /api/orgs/{organization_id}/projects
Content-Type: application/json
```

Body:

```json
{
  "name": "MICA Application",
  "description": "Shared working set for the initial submission."
}
```

### 4. Get one project

```http
GET /api/orgs/{organization_id}/projects/{project_id}
```

### 5. Update project

```http
PATCH /api/orgs/{organization_id}/projects/{project_id}
Content-Type: application/json
```

Body:

```json
{
  "name": "MICA Filing",
  "description": "Updated description"
}
```

Both fields are optional, but at least one must be sent.

### 6. Delete project

```http
DELETE /api/orgs/{organization_id}/projects/{project_id}
```

Important:

- the backend returns `409` if the project still has documents
- the UI should tell the user to move or delete those documents first

### 7. List all documents

```http
GET /api/orgs/{organization_id}/documents
```

Optional filter:

```http
GET /api/orgs/{organization_id}/documents?project_id={project_id}
```

Each document now includes project metadata:

```json
{
  "document_id": "doc_123",
  "organization_id": "org_123",
  "title": "Operations Manual",
  "project_id": "550e8400-e29b-41d4-a716-446655440000",
  "project": {
    "project_id": "550e8400-e29b-41d4-a716-446655440000",
    "organization_id": "org_123",
    "name": "MICA Application",
    "description": "Shared working set for the initial submission.",
    "created_at": "2026-04-01T12:00:00Z",
    "created_by": "user_123",
    "updated_at": "2026-04-01T12:30:00Z",
    "updated_by": "user_123"
  },
  "created_at": "2026-04-01T12:10:00Z",
  "created_by": "user_123",
  "updated_at": "2026-04-01T12:30:00Z",
  "my_role": "owner",
  "current_version": {
    "version_id": "ver_123",
    "organization_id": "org_123",
    "document_id": "doc_123",
    "version_no": 2,
    "content_hash": "sha256...",
    "object_key": "objects/sha256....md",
    "size_bytes": 12345,
    "created_at": "2026-04-01T12:30:00Z",
    "created_by": "user_123",
    "message": "Updated wording",
    "parent_version_id": "ver_122",
    "compliance_result": null
  }
}
```

### 8. List documents for a single project

```http
GET /api/orgs/{organization_id}/projects/{project_id}/documents
```

This returns the same shape as the normal document list endpoint.

### 9. Upload a new document into a project

```http
POST /api/documents/upload
Content-Type: multipart/form-data
```

Form fields:

- `organization_id` required from `/api/me`
- `project_id` required for new documents
- `file` required
- `title` optional
- `message` optional

Important:

- `project_id` is now required when creating a new document
- the UI must force the user to choose a project before upload

Successful response:

```json
{
  "organization_id": "org_123",
  "project_id": "550e8400-e29b-41d4-a716-446655440000",
  "document_id": "doc_123",
  "version_id": "ver_123",
  "version_no": 1,
  "content_hash": "sha256..."
}
```

### 10. Upload a new version of an existing document

Use the same endpoint:

```http
POST /api/documents/upload
Content-Type: multipart/form-data
```

Form fields:

- `organization_id` required
- `document_id` required
- `file` required
- `title` optional
- `message` optional
- `project_id` optional

If `project_id` is sent during a version upload, the backend will also move the document to that project.

### 11. Move an existing document to another project

```http
PATCH /api/orgs/{organization_id}/documents/{document_id}/project
Content-Type: application/json
```

Body:

```json
{
  "project_id": "e7f8aeb8-a57f-4c7a-83ca-f2e6a5b8e8a0"
}
```

Use this from document row actions, document details, or drag/drop style UI.

### 12. Existing document detail endpoints

These now include `project_id` and `project` in the `document` object:

```http
GET /api/orgs/{organization_id}/documents/{document_id}
GET /api/orgs/{organization_id}/documents/{document_id}/versions
GET /api/orgs/{organization_id}/documents/{document_id}/versions/{version_no}
```

## Error Handling

Handle these cases explicitly:

- `400` invalid request body, missing required project selection, or empty project name
- `403` wrong workspace or no access
- `404` project or document not found
- `409` delete blocked because the project still contains documents

## Suggested UI Flow

1. Load `/api/me`.
2. Load `/api/orgs/{organization_id}/projects`.
3. Select the first project by default, or show an empty state if none exist.
4. Load `/api/orgs/{organization_id}/projects/{project_id}/documents`.
5. For new uploads, require project selection before enabling submit.
6. For delete project, if the backend returns `409`, prompt the user to move documents out first.

## Acceptance Criteria

1. A user can create, rename, and delete empty projects.
2. A user can upload a new document only after selecting a project.
3. A user can browse documents by project.
4. A user can move an existing document between projects.
5. Document detail and list screens visibly show the current project.
6. No folder endpoints are used by the frontend anymore.
