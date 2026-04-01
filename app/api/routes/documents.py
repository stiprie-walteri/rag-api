import os
import logging
import tempfile
import yaml
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from app.core.auth import AuthContext, get_auth_context
from app.api.dependencies import require_docstore, sync_authenticated_org
from app.services.storage.service import (
    DocumentNotFoundError,
    OrganizationMismatchError,
    DocumentAccessDeniedError,
    DocumentVersionNotFoundError,
    DocumentHasNoVersionsError,
    ProjectNotFoundError,
)
from app.services.pdf.chunking import chunk_pdf
from app.services.pdf.converter import convert_pdf_to_markdown
from app.services.evaluation.agent import evaluate_task_with_agent, TaskEvaluationResult

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Documents"])

class UploadDocumentResponse(BaseModel):
    organization_id: str
    project_id: str | None = None
    document_id: str
    version_id: str
    version_no: int
    content_hash: str


class ProjectSummary(BaseModel):
    project_id: str
    organization_id: str
    name: str
    description: str | None = None
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str


class DocumentVersionMetadata(BaseModel):
    version_id: str
    organization_id: str
    document_id: str
    version_no: int
    content_hash: str
    object_key: str
    size_bytes: int
    created_at: datetime
    created_by: str
    message: str | None = None
    parent_version_id: str | None = None
    compliance_result: dict | None = None


class DocumentMetadata(BaseModel):
    document_id: str
    organization_id: str
    title: str | None = None
    project_id: str | None = None
    project: ProjectSummary | None = None
    created_at: datetime
    created_by: str
    updated_at: datetime
    my_role: str | None = None


class DocumentListItem(DocumentMetadata):
    current_version: DocumentVersionMetadata | None = None


class DocumentListResponse(BaseModel):
    items: list[DocumentListItem]
    limit: int
    offset: int
    next_offset: int | None = None


class DocumentContentResponse(BaseModel):
    document: DocumentMetadata
    version: DocumentVersionMetadata
    content_md: str


class DocumentVersionsResponse(BaseModel):
    organization_id: str
    document_id: str
    items: list[DocumentVersionMetadata]
    limit: int
    offset: int
    next_offset: int | None = None


class DocumentChunk(BaseModel):
    id: str
    organization_id: str
    document_id: str
    version_id: str
    chunk_level: int
    title: str | None = None
    start_page: int
    end_page: int
    text_content: str
    created_at: datetime


class DocumentChunksResponse(BaseModel):
    organization_id: str
    document_id: str
    version_id: str
    chunks: list[DocumentChunk]


class DocstoreGcResponse(BaseModel):
    dry_run: bool
    scanned_objects: int
    referenced_objects: int
    unreferenced_objects: int
    candidate_keys: list[str]
    deleted_keys: list[str]


class DocumentEvaluationRequest(BaseModel):
    tasks: list[list[str]] | None = None


class DocumentEvaluationResponse(BaseModel):
    organization_id: str
    document_id: str
    version_id: str
    results: list[TaskEvaluationResult]


def _is_markdown_upload(file: UploadFile) -> bool:
    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()
    return (
        filename.endswith(".md")
        or filename.endswith(".markdown")
        or "markdown" in content_type
        or content_type == "text/plain"
    )


def _is_pdf_upload(file: UploadFile) -> bool:
    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()
    return filename.endswith(".pdf") or content_type == "application/pdf"


def _to_version_metadata(raw: dict) -> DocumentVersionMetadata:
    return DocumentVersionMetadata(**raw)


def _to_project_summary(raw: dict | None) -> ProjectSummary | None:
    if raw is None:
        return None
    return ProjectSummary(**raw)


def _to_document_metadata(raw: dict) -> DocumentMetadata:
    return DocumentMetadata(
        document_id=raw["document_id"],
        organization_id=raw["organization_id"],
        title=raw.get("title"),
        project_id=raw.get("project_id"),
        project=_to_project_summary(raw.get("project")),
        created_at=raw["created_at"],
        created_by=raw["created_by"],
        updated_at=raw["updated_at"],
        my_role=raw.get("my_role"),
    )


def _to_document_list_item(raw: dict) -> DocumentListItem:
    current_version = raw.get("current_version")
    return DocumentListItem(
        document_id=raw["document_id"],
        organization_id=raw["organization_id"],
        title=raw.get("title"),
        project_id=raw.get("project_id"),
        project=_to_project_summary(raw.get("project")),
        created_at=raw["created_at"],
        created_by=raw["created_by"],
        updated_at=raw["updated_at"],
        my_role=raw.get("my_role"),
        current_version=_to_version_metadata(current_version) if current_version else None,
    )


@router.post("/api/documents/upload", response_model=UploadDocumentResponse)
async def upload_document(
    organization_id: str | None = Form(default=None),
    file: UploadFile = File(...),
    document_id: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
    title: str | None = Form(default=None),
    message: str | None = Form(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=True,
    )
    is_md = _is_markdown_upload(file)
    is_pdf = _is_pdf_upload(file)
    if not is_md and not is_pdf:
        raise HTTPException(status_code=400, detail="Only Markdown and PDF files are accepted.")

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    
    chunks = None
    if is_pdf:
        try:
            try:
                chunks = chunk_pdf(raw_bytes)
            except Exception as exc:
                logger.warning("Failed to extract structural chunks from PDF: %s", exc)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                tmp_pdf.write(raw_bytes)
                tmp_pdf_path = tmp_pdf.name
            tmp_md_path = tmp_pdf_path.replace(".pdf", ".md")
            convert_pdf_to_markdown(tmp_pdf_path, tmp_md_path)
            with open(tmp_md_path, "r", encoding="utf-8") as f:
                markdown_bytes = f.read().encode("utf-8")
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Failed to convert PDF to Markdown: {exc}") from exc
        finally:
            for p in [tmp_pdf_path, tmp_md_path]:
                if p and os.path.exists(p):
                    os.unlink(p)
    else:
        markdown_bytes = raw_bytes

    try:
        result = service.create_version(
            organization_id=org_context["organization_id"],
            actor_user_id=auth.user_id,
            markdown_bytes=markdown_bytes,
            document_id=document_id,
            project_id=project_id,
            title=title,
            message=message,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentVersionNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if chunks:
        try:
            service.save_document_chunks(
                organization_id=org_context["organization_id"],
                document_id=result["document_id"],
                version_id=result["version_id"],
                chunks=chunks,
            )
        except Exception as exc:
            logger.warning("Failed to save document chunks: %s", exc)

    return UploadDocumentResponse(
        organization_id=org_context["organization_id"],
        project_id=result.get("project_id"),
        document_id=result["document_id"],
        version_id=result["version_id"],
        version_no=result["version_no"],
        content_hash=result["content_hash"],
    )


@router.get("/api/orgs/{organization_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    organization_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    project_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        items_raw = service.list_documents(
            organization_id=org_context["organization_id"],
            actor_user_id=auth.user_id,
            limit=limit,
            offset=offset,
            project_id=project_id,
        )
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    items = [_to_document_list_item(item) for item in items_raw]
    next_offset = offset + len(items) if len(items) == limit else None
    return DocumentListResponse(items=items, limit=limit, offset=offset, next_offset=next_offset)


@router.get("/api/orgs/{organization_id}/documents/{document_id}", response_model=DocumentContentResponse)
async def get_document_current(
    organization_id: str,
    document_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        result = service.get_document_current(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            actor_user_id=auth.user_id,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (DocumentHasNoVersionsError, DocumentVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return DocumentContentResponse(
        document=_to_document_metadata(result["document"]),
        version=_to_version_metadata(result["version"]),
        content_md=result["content_bytes"].decode("utf-8"),
    )


@router.get(
    "/api/orgs/{organization_id}/documents/{document_id}/versions/{version_no}",
    response_model=DocumentContentResponse,
)
async def get_document_version(
    organization_id: str,
    document_id: str,
    version_no: int,
    auth: AuthContext = Depends(get_auth_context),
):
    if version_no <= 0:
        raise HTTPException(status_code=400, detail="version_no must be greater than 0.")

    service = require_docstore()
    org_context = sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        result = service.get_document_version(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            version_no=version_no,
            actor_user_id=auth.user_id,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return DocumentContentResponse(
        document=_to_document_metadata(result["document"]),
        version=_to_version_metadata(result["version"]),
        content_md=result["content_bytes"].decode("utf-8"),
    )


@router.get("/api/orgs/{organization_id}/documents/{document_id}/versions", response_model=DocumentVersionsResponse)
async def list_document_versions(
    organization_id: str,
    document_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        items_raw = service.list_versions(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            actor_user_id=auth.user_id,
            limit=limit,
            offset=offset,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    items = [_to_version_metadata(item) for item in items_raw]
    next_offset = offset + len(items) if len(items) == limit else None
    return DocumentVersionsResponse(
        organization_id=org_context["organization_id"],
        document_id=document_id,
        items=items,
        limit=limit,
        offset=offset,
        next_offset=next_offset,
    )


@router.get(
    "/api/orgs/{organization_id}/documents/{document_id}/versions/{version_no}/chunks",
    response_model=DocumentChunksResponse,
)
async def get_document_version_chunks(
    organization_id: str,
    document_id: str,
    version_no: int,
    title: str | None = Query(default=None, description="Filter chunks by exact title"),
    chunk_level: int | None = Query(default=None, description="Filter chunks by hierarchical level"),
    auth: AuthContext = Depends(get_auth_context),
):
    if version_no <= 0:
        raise HTTPException(status_code=400, detail="version_no must be greater than 0.")

    service = require_docstore()
    org_context = sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )

    try:
        result = service.get_document_version(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            version_no=version_no,
            actor_user_id=auth.user_id,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    version_id = result["version"]["version_id"]

    try:
        chunks_raw = service.get_document_chunks(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            version_id=version_id,
            actor_user_id=auth.user_id,
            title=title,
            chunk_level=chunk_level,
        )
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    chunks = [DocumentChunk(**chunk) for chunk in chunks_raw]

    return DocumentChunksResponse(
        organization_id=org_context["organization_id"],
        document_id=document_id,
        version_id=version_id,
        chunks=chunks,
    )


@router.post(
    "/api/orgs/{organization_id}/documents/{document_id}/versions/{version_no}/evaluate",
    response_model=DocumentEvaluationResponse,
)
async def evaluate_document_tasks(
    organization_id: str,
    document_id: str,
    version_no: int,
    request: DocumentEvaluationRequest,
    template_id: str | None = Query(default=None, description="Optional template ID to use for system prompt and tasks"),
    auth: AuthContext = Depends(get_auth_context),
):
    if version_no <= 0:
        raise HTTPException(status_code=400, detail="version_no must be greater than 0.")

    tasks_to_run = request.tasks or []
    system_prompt_override = None

    if template_id:
        template_data = None
        templates_dir = Path(os.getenv("LEGISLATION_TEMPLATES_DIR", "legislation-templates"))
        if templates_dir.exists() and templates_dir.is_dir():
            for file_path in templates_dir.glob("*.yaml"):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                        if data.get("id", file_path.stem) == template_id:
                            template_data = data
                            break
                except Exception as exc:
                    logger.warning("Failed to load template %s: %s", file_path, exc)
                    
        if not template_data:
            raise HTTPException(status_code=404, detail=f"Template {template_id} not found.")
            
        system_prompt_override = template_data.get("system_prompt")
        if not request.tasks:
            tasks_to_run = template_data.get("Tasks", [])

    if not tasks_to_run:
        raise HTTPException(status_code=400, detail="No tasks provided and no template tasks found.")

    service = require_docstore()
    org_context = sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )

    try:
        ver_result = service.get_document_version(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            version_no=version_no,
            actor_user_id=auth.user_id,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    version_id = ver_result["version"]["version_id"]

    try:
        chunks_raw = service.get_document_chunks(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            version_id=version_id,
            actor_user_id=auth.user_id,
        )
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    results = []
    # For a real scalable system these could be evaluated concurrently,
    # but sequential processing is sufficient to demonstrate the OpenRouter agent feature.
    for task_list in tasks_to_run:
        result = evaluate_task_with_agent(
            task_list=task_list, 
            chunks=chunks_raw, 
            system_prompt_override=system_prompt_override
        )
        results.append(result)

    return DocumentEvaluationResponse(
        organization_id=org_context["organization_id"],
        document_id=document_id,
        version_id=version_id,
        results=results,
    )


class DocumentDeleteResponse(BaseModel):
    document_id: str
    organization_id: str
    chunks_deleted: int
    versions_deleted: int
    objects_deleted: int
    objects_failed: int


@router.delete(
    "/api/orgs/{organization_id}/documents/{document_id}",
    response_model=DocumentDeleteResponse,
)
async def delete_document(
    organization_id: str,
    document_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )

    try:
        result = service.delete_document(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            actor_user_id=auth.user_id,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return DocumentDeleteResponse(**result)


class SaveComplianceRequest(BaseModel):
    result: dict


@router.post(
    "/api/orgs/{organization_id}/documents/{document_id}/versions/{version_no}/compliance",
    status_code=204,
)
async def save_compliance_result(
    organization_id: str,
    document_id: str,
    version_no: int,
    body: SaveComplianceRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )

    try:
        result = service.get_document_version(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            version_no=version_no,
            actor_user_id=auth.user_id,
        )
        service.save_compliance_result(
            organization_id=org_context["organization_id"],
            version_id=result["version"]["version_id"],
            result=body.result,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OrganizationMismatchError, DocumentAccessDeniedError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/admin/docstore/gc", response_model=DocstoreGcResponse)
async def run_docstore_gc(
    dry_run: bool = Query(default=True),
    max_delete: int = Query(default=100, ge=1, le=5000),
):
    service = require_docstore()
    result = service.gc_unreferenced_objects(dry_run=dry_run, max_delete=max_delete)
    return DocstoreGcResponse(**result)
