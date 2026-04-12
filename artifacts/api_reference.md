# API Reference: Document Indexing & Suggestions

This document describes the updated Pydantic models and API endpoints that provide character-level offsets for document edits and suggestions.

## Data Models

### `SuggestedInsertLocation` 
Used within `DocumentationIssue` to describe where an AI-suggested fix should be applied.

| Field | Type | Description |
| :--- | :--- | :--- |
| `action` | `str` | Type of edit: `insert_after_section`, `append_to_section`, `replace_text`, or `create_new_section`. |
| `target_section_id` | `str?` | TOC index or section ID. |
| `target_section_title`| `str?` | Title of the target section. |
| `anchor_quote` | `str?` | Verbatim text from the document to anchor the edit. |
| `placement` | `str` | `before`, `after`, `replace`, or `end_of_section`. |
| **`start_index`** | **`int?`** | **(New)** The starting character offset in the original markdown. |
| **`end_index`** | **`int?`** | **(New)** The ending character offset in the original markdown. |

### `SuggestionApplicationResult` 
Returned after a suggestion has been applied to a document version.

| Field | Type | Description |
| :--- | :--- | :--- |
| `issue_id` | `str` | Stable ID for the issue. |
| `status` | `str` | `applied`, `skipped`, or `failed`. |
| `match_strategy` | `str?` | Logic used to find the location (e.g., `anchor_quote`). |
| **`start_index`** | **`int?`** | **(New)** Character offset where the edit was applied. |
| **`end_index`** | **`int?`** | **(New)** Character offset of the original text that was replaced. |

---

## Impacted Endpoints

### 1. Document & Project Evaluation Status
Endpoints that return `TaskEvaluationResult` will now include resolved character offsets in the `Issues` list.

- **Document**: `GET /api/orgs/{org_id}/documents/{doc_id}/evaluation/status` 
- **Project**: `GET /api/orgs/{org_id}/projects/{proj_id}/evaluation/status` 

### 2. Apply Suggestions
The response from applying suggestions now includes the exact indices where each patch was attempted.

- **Endpoint**: `POST /api/orgs/{org_id}/documents/{doc_id}/versions/{version_no}/suggestions/apply` 

> [!TIP]
> Frontend clients should use `start_index` and `end_index` to handle text highlighting and precise "Apply" button placement, rather than relying solely on string matching, which can be ambiguous if multiple identical quotes exist.
