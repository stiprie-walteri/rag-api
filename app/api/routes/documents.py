import asyncio
import os
import logging
import tempfile
import uuid
from datetime import datetime
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
from app.services.storage.service import DocumentStorageService
from app.services.pdf.chunking import chunk_pdf
from app.services.pdf.converter import convert_pdf_to_markdown
from app.services.evaluation.agent import (
    TaskEvaluationResult,
    build_exploratory_document_summary,
    evaluate_task_with_agent,
)
from app.services.evaluation import state as eval_state
from app.services.legislation.templates import get_legislation_template

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
    legislation_template_ids: list[str] = []
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


class EvaluationStartedResponse(BaseModel):
    job_id: str
    document_id: str
    version_id: str
    status: str = "running"


class EvaluationStatusResponse(BaseModel):
    job_id: str
    status: str
    organization_id: str
    document_id: str
    version_id: str
    total_tasks: int
    completed_count: int
    current_task: list[str] | None = None
    results: list[TaskEvaluationResult]
    error: str | None = None
    started_at: datetime
    updated_at: datetime


class DocumentEvaluationSummary(BaseModel):
    document_id: str
    title: str | None = None
    is_analyzing: bool
    job_id: str | None = None
    status: str | None = None
    total_tasks: int | None = None
    completed_count: int | None = None
    current_task: list[str] | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    compliance_result: dict | None = None


class DocumentEvaluationStatusesResponse(BaseModel):
    items: list[DocumentEvaluationSummary]


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


class BatchUploadFailure(BaseModel):
    filename: str
    error: str


class BatchUploadDocumentResponse(BaseModel):
    organization_id: str
    project_id: str | None = None
    documents: list[UploadDocumentResponse]
    failed: list[BatchUploadFailure]


MAX_BATCH_UPLOAD_SIZE = 20


async def _process_single_upload(
    *,
    service,
    file: UploadFile,
    organization_id: str,
    actor_user_id: str,
    project_id: str | None,
    document_id: str | None,
    title: str | None,
    message: str | None,
) -> UploadDocumentResponse:
    is_md = _is_markdown_upload(file)
    is_pdf = _is_pdf_upload(file)
    if not is_md and not is_pdf:
        raise HTTPException(status_code=400, detail="Only Markdown and PDF files are accepted.")

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    chunks = None
    tmp_pdf_path = None
    tmp_md_path = None
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
        result = await service.create_version(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
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
            await service.save_document_chunks(
                organization_id=organization_id,
                document_id=result["document_id"],
                version_id=result["version_id"],
                chunks=chunks,
            )
        except Exception as exc:
            logger.warning("Failed to save document chunks: %s", exc)

    return UploadDocumentResponse(
        organization_id=organization_id,
        project_id=result.get("project_id"),
        document_id=result["document_id"],
        version_id=result["version_id"],
        version_no=result["version_no"],
        content_hash=result["content_hash"],
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
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=True,
    )
    return await _process_single_upload(
        service=service,
        file=file,
        organization_id=org_context["organization_id"],
        actor_user_id=auth.user_id,
        project_id=project_id,
        document_id=document_id,
        title=title,
        message=message,
    )


@router.post("/api/documents/upload-batch", response_model=BatchUploadDocumentResponse)
async def upload_documents_batch(
    organization_id: str | None = Form(default=None),
    files: list[UploadFile] = File(...),
    project_id: str | None = Form(default=None),
    message: str | None = Form(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    if len(files) > MAX_BATCH_UPLOAD_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {MAX_BATCH_UPLOAD_SIZE} files per batch.",
        )
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    service = require_docstore()
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=True,
    )
    resolved_org_id = org_context["organization_id"]

    documents: list[UploadDocumentResponse] = []
    failed: list[BatchUploadFailure] = []

    for file in files:
        filename = file.filename or "unknown"
        title = os.path.splitext(filename)[0]
        try:
            result = await _process_single_upload(
                service=service,
                file=file,
                organization_id=resolved_org_id,
                actor_user_id=auth.user_id,
                project_id=project_id,
                document_id=None,
                title=title,
                message=message,
            )
            documents.append(result)
        except HTTPException as exc:
            failed.append(BatchUploadFailure(filename=filename, error=exc.detail))
        except Exception as exc:
            logger.warning("Batch upload failed for file %s: %s", filename, exc)
            failed.append(BatchUploadFailure(filename=filename, error=str(exc)))

    return BatchUploadDocumentResponse(
        organization_id=resolved_org_id,
        project_id=project_id,
        documents=documents,
        failed=failed,
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
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        items_raw = await service.list_documents(
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


@router.get(
    "/api/orgs/{organization_id}/documents/evaluation-statuses",
    response_model=DocumentEvaluationStatusesResponse,
)
async def list_document_evaluation_statuses(
    organization_id: str,
    project_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        items_raw = await service.list_documents(
            organization_id=org_context["organization_id"],
            actor_user_id=auth.user_id,
            limit=200,
            offset=0,
            project_id=project_id,
        )
    except (DocumentAccessDeniedError, OrganizationMismatchError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    document_ids = [item["document_id"] for item in items_raw]
    job_states = await eval_state.get_job_states_for_documents(document_ids)

    summaries: list[DocumentEvaluationSummary] = []
    for item in items_raw:
        doc_id = item["document_id"]
        job = job_states.get(doc_id)
        compliance = _augment_compliance_result(item.get("current_version") and item["current_version"].get("compliance_result"))

        if job:
            summaries.append(DocumentEvaluationSummary(
                document_id=doc_id,
                title=item.get("title"),
                is_analyzing=job["status"] == "running",
                job_id=job["job_id"],
                status=job["status"],
                total_tasks=job["total_tasks"],
                completed_count=job["completed_count"],
                current_task=job.get("current_task"),
                started_at=job["started_at"],
                updated_at=job["updated_at"],
                compliance_result=compliance,
            ))
        else:
            summaries.append(DocumentEvaluationSummary(
                document_id=doc_id,
                title=item.get("title"),
                is_analyzing=False,
                compliance_result=compliance,
            ))

    return DocumentEvaluationStatusesResponse(items=summaries)


class DocumentComplianceResultResponse(BaseModel):
    document_id: str
    version_id: str
    compliance_result: dict | None = None


def _augment_compliance_result(comp_result: dict | None) -> dict | None:
    if not comp_result or "results" not in comp_result:
        return comp_result
    
    results_list = comp_result["results"]
    total_tasks = len(results_list)
    if total_tasks > 0:
        correct_count = 0
        for r in results_list:
            is_correct = bool(r.get("exists")) and not r.get("missing_sections") and not r.get("incorrect_sections")
            r["is_correct"] = is_correct
            if is_correct:
                correct_count += 1
        
        incorrect_tasks = total_tasks - correct_count
        comp_result["correctness_score"] = int(((total_tasks - incorrect_tasks) / total_tasks) * 100)
    
    return comp_result

@router.get(
    "/api/orgs/{organization_id}/documents/{document_id}/compliance",
    response_model=DocumentComplianceResultResponse,
)
async def get_document_compliance_result(
    organization_id: str,
    document_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        result = await service.get_document_current(
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

    comp_result = _augment_compliance_result(result["version"].get("compliance_result"))

    return DocumentComplianceResultResponse(
        document_id=document_id,
        version_id=result["version"]["version_id"],
        compliance_result=comp_result,
    )


@router.get("/api/orgs/{organization_id}/documents/{document_id}", response_model=DocumentContentResponse)
async def get_document_current(
    organization_id: str,
    document_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        result = await service.get_document_current(
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

    version_metadata = _to_version_metadata(result["version"])
    version_metadata.compliance_result = _augment_compliance_result(version_metadata.compliance_result)

    return DocumentContentResponse(
        document=_to_document_metadata(result["document"]),
        version=version_metadata,
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
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        result = await service.get_document_version(
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

    version_metadata = _to_version_metadata(result["version"])
    version_metadata.compliance_result = _augment_compliance_result(version_metadata.compliance_result)

    return DocumentContentResponse(
        document=_to_document_metadata(result["document"]),
        version=version_metadata,
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
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )
    try:
        items_raw = await service.list_versions(
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
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )

    try:
        result = await service.get_document_version(
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
        chunks_raw = await service.get_document_chunks(
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


_AGENT_CONCURRENCY = int(os.getenv("AGENT_CONCURRENCY", "3"))


def _build_template_run(
    *,
    template_data: dict,
    override_tasks: list[list[str]] | None = None,
) -> dict:
    tasks = override_tasks if override_tasks is not None else (template_data.get("Tasks") or [])
    return {
        "template_id": template_data["id"],
        "template_name": template_data["name"],
        "tasks": tasks,
        "system_prompt_override": template_data.get("system_prompt"),
        "references": template_data.get("references"),
    }


async def _run_evaluation_background(
    *,
    job_id: str,
    document_id: str,
    version_id: str,
    organization_id: str,
    evaluation_runs: list[dict],
    chunks_raw: list[dict],
    service: DocumentStorageService,
) -> None:
    try:
        exploratory_summary = await build_exploratory_document_summary(chunks_raw)
        total_tasks = sum(len(run["tasks"]) for run in evaluation_runs)
        results: list[TaskEvaluationResult | None] = [None] * total_tasks
        completed_count = 0
        state_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(_AGENT_CONCURRENCY)
        result_index = 0
        run_summaries: list[dict] = []
        scheduled_runs: list[tuple[int, dict, list[str]]] = []

        for run in evaluation_runs:
            run_result_start = result_index
            for task_list in run["tasks"]:
                scheduled_runs.append((result_index, run, task_list))
                result_index += 1
            run_summaries.append(
                {
                    "template_id": run["template_id"],
                    "template_name": run["template_name"],
                    "task_count": len(run["tasks"]),
                    "result_indexes": list(range(run_result_start, result_index)),
                }
            )

        async def _run_one(idx: int, run: dict, task_list: list[str]) -> None:
            nonlocal completed_count
            async with semaphore:
                logger.info(
                    "[eval] Starting task %d/%d for %s: %s",
                    idx + 1,
                    total_tasks,
                    run["template_id"],
                    task_list[0][:80],
                )
                async with state_lock:
                    await eval_state.update_progress(job_id, current_task=task_list, completed_count=completed_count)
                result = await evaluate_task_with_agent(
                    task_list=task_list,
                    chunks=chunks_raw,
                    system_prompt_override=run.get("system_prompt_override"),
                    references=run.get("references"),
                    exploratory_summary=exploratory_summary,
                )
                result.legislation_id = run["template_id"]
                result.legislation_name = run["template_name"]
                results[idx] = result
                async with state_lock:
                    completed_count += 1
                    await eval_state.append_result(job_id, result=result.model_dump(), completed_count=completed_count)

        await asyncio.gather(*(_run_one(i, run, task_list) for i, run, task_list in scheduled_runs))

        await eval_state.complete_job(job_id)

        try:
            final_results = [r.model_dump() for r in results if r is not None]
            await service.save_compliance_result(
                organization_id=organization_id,
                version_id=version_id,
                result={
                    "evaluation_job_id": job_id,
                    "exploratory_summary": exploratory_summary,
                    "legislations": [
                        {
                            "template_id": run_summary["template_id"],
                            "template_name": run_summary["template_name"],
                            "task_count": run_summary["task_count"],
                            "results": [
                                results[idx].model_dump()
                                for idx in run_summary["result_indexes"]
                                if results[idx] is not None
                            ],
                        }
                        for run_summary in run_summaries
                    ],
                    "results": final_results,
                },
            )
        except Exception as exc:
            logger.warning("Evaluation job %s: failed to persist results to Postgres: %s", job_id, exc)

    except Exception as exc:
        logger.exception("Evaluation job %s failed: %s", job_id, exc)
        await eval_state.fail_job(job_id, str(exc))
    finally:
        await eval_state.release_lock(document_id)


@router.post(
    "/api/orgs/{organization_id}/documents/{document_id}/versions/{version_no}/evaluate",
    response_model=EvaluationStartedResponse,
    status_code=202,
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

    if await eval_state.is_locked(document_id):
        raise HTTPException(
            status_code=409,
            detail=f"An evaluation is already running for document {document_id}.",
        )

    service = require_docstore()
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )

    try:
        ver_result = await service.get_document_version(
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
    project = ver_result["document"].get("project") or {}
    project_template_ids = project.get("legislation_template_ids") or []

    evaluation_runs: list[dict] = []
    if template_id:
        template_data = get_legislation_template(template_id)
        if not template_data:
            raise HTTPException(status_code=404, detail=f"Template {template_id} not found.")
        evaluation_runs.append(_build_template_run(template_data=template_data, override_tasks=request.tasks))
    elif request.tasks:
        evaluation_runs.append(
            {
                "template_id": "manual",
                "template_name": "Manual Tasks",
                "tasks": request.tasks,
                "system_prompt_override": None,
                "references": None,
            }
        )
    else:
        for project_template_id in project_template_ids:
            template_data = get_legislation_template(project_template_id)
            if not template_data:
                raise HTTPException(
                    status_code=400,
                    detail=f"Project references unknown legislation template {project_template_id}.",
                )
            evaluation_runs.append(_build_template_run(template_data=template_data))

    total_tasks = sum(len(run["tasks"]) for run in evaluation_runs)
    if total_tasks == 0:
        raise HTTPException(
            status_code=400,
            detail="No tasks provided and no project legislation templates are configured.",
        )

    try:
        chunks_raw = await service.get_document_chunks(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            version_id=version_id,
            actor_user_id=auth.user_id,
        )
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    job_id = str(uuid.uuid4())

    if not await eval_state.acquire_lock(document_id, job_id):
        raise HTTPException(
            status_code=409,
            detail=f"An evaluation is already running for document {document_id}.",
        )

    await eval_state.start_job(
        job_id=job_id,
        target_key=document_id,
        organization_id=org_context["organization_id"],
        target_type="document",
        target_id=document_id,
        version_id=version_id,
        total_tasks=total_tasks,
    )

    asyncio.create_task(
        _run_evaluation_background(
            job_id=job_id,
            document_id=document_id,
            version_id=version_id,
            organization_id=org_context["organization_id"],
            evaluation_runs=evaluation_runs,
            chunks_raw=chunks_raw,
            service=service,
        )
    )

    return EvaluationStartedResponse(
        job_id=job_id,
        document_id=document_id,
        version_id=version_id,
        status="running",
    )


@router.get(
    "/api/orgs/{organization_id}/documents/{document_id}/evaluation/status",
    response_model=EvaluationStatusResponse,
)
async def get_evaluation_status(
    organization_id: str,
    document_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )

    job_id = await eval_state.get_current_job_id(document_id)
    if not job_id:
        raise HTTPException(status_code=404, detail="No evaluation found for this document.")

    job = await eval_state.get_job_state(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Evaluation job state has expired.")

    if job["organization_id"] != org_context["organization_id"]:
        raise HTTPException(status_code=403, detail="Access denied.")

    return EvaluationStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        organization_id=job["organization_id"],
        document_id=job["document_id"],
        version_id=job["version_id"],
        total_tasks=job["total_tasks"],
        completed_count=job["completed_count"],
        current_task=job.get("current_task"),
        results=[TaskEvaluationResult(**r) for r in job.get("results", [])],
        error=job.get("error"),
        started_at=job["started_at"],
        updated_at=job["updated_at"],
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
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )

    try:
        result = await service.delete_document(
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
    org_context = await sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=False,
    )

    try:
        result = await service.get_document_version(
            organization_id=org_context["organization_id"],
            document_id=document_id,
            version_no=version_no,
            actor_user_id=auth.user_id,
        )
        await service.save_compliance_result(
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
    result = await service.gc_unreferenced_objects(dry_run=dry_run, max_delete=max_delete)
    return DocstoreGcResponse(**result)
