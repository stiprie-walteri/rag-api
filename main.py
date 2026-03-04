import json
import logging
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from compare_chunks import IssueList, call_openai_for_issues, find_sections_for_code, init_openai_from_env
from document_storage import (
    DocumentAccessDeniedError,
    DocumentHasNoVersionsError,
    DocumentNotFoundError,
    OrganizationNotFoundError,
    DocumentStorageSettings,
    DocumentStorageService,
    DocumentVersionNotFoundError,
    OrganizationMismatchError,
)
from get_submission_chunks import get_submission_by_codes
from legislation_util.find_sections import compute_metrics, load_legislation_unique_sections, parse_submission_codes
from legislation_util.get_legislation_by_section import get_subsections_for_code, load_legislation
from parse_legislation_codes import LegislationCodeParser
from pdf_to_markdown import convert_pdf_to_markdown
from clerk_auth import AuthContext, ClerkAuthMiddleware, get_auth_context


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


MOCK_RESPONSE_FILE = Path("full_response.json")
mock_response = None
if MOCK_RESPONSE_FILE.exists():
    try:
        with open(MOCK_RESPONSE_FILE, "r", encoding="utf-8") as f:
            mock_response = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load mock response from %s: %s", MOCK_RESPONSE_FILE, exc)

jobs: dict[str, dict] = {}


app = FastAPI(
    title="Python API Template",
    description="A simple Python API template with health check endpoint",
    version="1.0.0",
)
# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Add Clerk authentication middleware
app.add_middleware(ClerkAuthMiddleware)


class HealthResponse(BaseModel):
    status: str
    message: str


class ParseResponse(BaseModel):
    markdown: str
    parsed_codes: dict
    metrics: dict
    issues: dict


class JobResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: ParseResponse | None = None


class UploadDocumentResponse(BaseModel):
    organization_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int
    content_hash: str


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


class DocumentMetadata(BaseModel):
    document_id: str
    organization_id: str
    title: str | None = None
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


class DocstoreGcResponse(BaseModel):
    dry_run: bool
    scanned_objects: int
    referenced_objects: int
    unreferenced_objects: int
    candidate_keys: list[str]
    deleted_keys: list[str]


class MeResponse(BaseModel):
    user_id: str
    clerk_org_id: str | None = None
    clerk_org_slug: str | None = None
    organization_id: str | None = None
    organization_role: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None


docstore_service: DocumentStorageService | None = None
try:
    docstore_service = DocumentStorageService(DocumentStorageSettings.from_env())
except ValueError as exc:
    logger.warning("Persistent document storage is disabled: %s", exc)


def _require_docstore() -> DocumentStorageService:
    if docstore_service is None:
        raise HTTPException(
            status_code=503,
            detail="Persistent document storage is not configured. Set POSTGRES_DSN and MinIO env vars.",
        )
    return docstore_service


def _sync_authenticated_org(
    service: DocumentStorageService,
    auth: AuthContext,
    *,
    requested_organization_id: str | None = None,
    create_if_missing: bool,
) -> dict:
    try:
        context = service.sync_authenticated_user(
            clerk_user_id=auth.user_id,
            clerk_org_id=auth.clerk_org_id,
            clerk_org_slug=auth.clerk_org_slug,
            clerk_org_role=auth.clerk_org_role,
            primary_email=auth.email,
            first_name=auth.first_name,
            last_name=auth.last_name,
            requested_organization_id=requested_organization_id,
            create_if_missing=create_if_missing,
        )
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OrganizationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if requested_organization_id is not None and context["organization_id"] != requested_organization_id:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Requested organization {requested_organization_id} does not match the active "
                f"private workspace {context['organization_id']}."
            ),
        )

    return context


def _is_markdown_upload(file: UploadFile) -> bool:
    filename = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()
    return (
        filename.endswith(".md")
        or filename.endswith(".markdown")
        or "markdown" in content_type
        or content_type == "text/plain"
    )


def _to_version_metadata(raw: dict) -> DocumentVersionMetadata:
    return DocumentVersionMetadata(**raw)


def _to_document_metadata(raw: dict) -> DocumentMetadata:
    return DocumentMetadata(
        document_id=raw["document_id"],
        organization_id=raw["organization_id"],
        title=raw.get("title"),
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
        created_at=raw["created_at"],
        created_by=raw["created_by"],
        updated_at=raw["updated_at"],
        my_role=raw.get("my_role"),
        current_version=_to_version_metadata(current_version) if current_version else None,
    )


@app.on_event("startup")
def initialize_docstore() -> None:
    if docstore_service is None:
        return
    docstore_service.initialize()


def process_legislation(job_id: str, file_content: bytes):
    jobs[job_id]["status"] = "processing"

    temp_pdf_path = None
    temp_md_path = None
    temp_parsed_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
            temp_pdf_path = temp_pdf.name
            temp_pdf.write(file_content)

        temp_md_path = temp_pdf_path.replace(".pdf", ".md")
        convert_pdf_to_markdown(temp_pdf_path, temp_md_path)

        with open(temp_md_path, "r", encoding="utf-8") as f:
            full_markdown = f.read()

        parser = LegislationCodeParser()
        parsed_codes = parser.parse_markdown(temp_md_path)

        legislation_path = Path("legislation_util/unique_sections_legislation.json")
        legislation_info = load_legislation_unique_sections(legislation_path)

        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as temp_parsed:
            json.dump(parsed_codes, temp_parsed)
            temp_parsed_path = temp_parsed.name

        submission_raw_codes, submission_norm_codes = parse_submission_codes(Path(temp_parsed_path))
        metrics = compute_metrics(legislation_info, submission_raw_codes, submission_norm_codes)

        init_openai_from_env()
        legislation = load_legislation(Path("legislation_util/legislation.json"))

        all_main_codes_found = metrics.get("all_main_codes_found", [])
        all_issues = []

        for code in all_main_codes_found:
            if not isinstance(code, str):
                continue

            leg_info = get_subsections_for_code(code, legislation)
            legislation_markdown = (leg_info.get("main_section") or "").strip()
            subsections_md = (leg_info.get("subsections_markdown") or "").strip()
            if subsections_md:
                if legislation_markdown:
                    legislation_markdown += "\n\n\n" + subsections_md
                else:
                    legislation_markdown = subsections_md

            submission_text = get_submission_by_codes([code], parsed_codes)
            if not submission_text.strip():
                continue

            result: IssueList = call_openai_for_issues(code, legislation_markdown, submission_text)
            submission_sections = find_sections_for_code(code, parsed_codes)

            for issue in result.issues:
                issue.main_code = issue.main_code or code
                issue.submission_sections = submission_sections
                all_issues.append(issue.model_dump())

        issues_output = {"issues": all_issues}

        result_data = ParseResponse(
            markdown=full_markdown,
            parsed_codes=parsed_codes,
            metrics=metrics,
            issues=issues_output,
        )
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["result"] = result_data

    except Exception as exc:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(exc)

    finally:
        for path in [temp_pdf_path, temp_md_path, temp_parsed_path]:
            if path and os.path.exists(path):
                os.unlink(path)


@app.get("/api/healthz", response_model=HealthResponse)
async def health_check():
    return HealthResponse(
        status="healthy",
        message="API is running successfully",
    )


@app.post("/api/parse-legislation", response_model=JobResponse)
async def parse_legislation(file: UploadFile = File(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")

    file_content = await file.read()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "result": None, "error": None}
    background_tasks.add_task(process_legislation, job_id, file_content)
    return JobResponse(job_id=job_id, status="pending")


@app.get("/api/parse-legislation-status/{job_id}", response_model=JobStatusResponse)
async def parse_legislation_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    if job["status"] == "completed":
        return JobStatusResponse(job_id=job_id, status="completed", result=job["result"])
    if job["status"] == "failed":
        raise HTTPException(status_code=500, detail=f"Job failed: {job['error']}")
    return JobStatusResponse(job_id=job_id, status=job["status"])


@app.get("/api/parse-legislation-mock", response_model=ParseResponse)
async def parse_legislation_mock():
    if mock_response:
        return ParseResponse(**mock_response)
    raise HTTPException(status_code=404, detail="Mock response not available")


@app.post("/documents/upload", response_model=UploadDocumentResponse)
@app.post("/api/documents/upload", response_model=UploadDocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    organization_id: uuid.UUID | None = Form(default=None),
    document_id: uuid.UUID | None = Form(default=None),
    title: str | None = Form(default=None),
    message: str | None = Form(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    service = _require_docstore()
    org_context = _sync_authenticated_org(
        service,
        auth,
        requested_organization_id=organization_id,
        create_if_missing=True,
    )
    if not _is_markdown_upload(file):
        raise HTTPException(status_code=400, detail="Only Markdown files are accepted.")

    markdown_bytes = await file.read()
    if not markdown_bytes:
        raise HTTPException(status_code=400, detail="Uploaded Markdown file is empty.")

    try:
        result = service.create_version(
            organization_id=org_context["organization_id"],
            actor_user_id=auth.user_id,
            markdown_bytes=markdown_bytes,
            document_id=document_id,
            title=title,
            message=message,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentVersionNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return UploadDocumentResponse(
        organization_id=org_context["organization_id"],
        document_id=result["document_id"],
        version_id=result["version_id"],
        version_no=result["version_no"],
        content_hash=result["content_hash"],
    )


@app.get("/orgs/{organization_id}/documents", response_model=DocumentListResponse)
@app.get("/api/orgs/{organization_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    organization_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
):
    service = _require_docstore()
    org_context = _sync_authenticated_org(
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
        )
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    items = [_to_document_list_item(item) for item in items_raw]
    next_offset = offset + len(items) if len(items) == limit else None
    return DocumentListResponse(items=items, limit=limit, offset=offset, next_offset=next_offset)


@app.get("/orgs/{organization_id}/documents/{document_id}", response_model=DocumentContentResponse)
@app.get("/api/orgs/{organization_id}/documents/{document_id}", response_model=DocumentContentResponse)
async def get_document_current(
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
):
    service = _require_docstore()
    org_context = _sync_authenticated_org(
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


@app.get("/orgs/{organization_id}/documents/{document_id}/versions/{version_no}", response_model=DocumentContentResponse)
@app.get(
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

    service = _require_docstore()
    org_context = _sync_authenticated_org(
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


@app.get("/orgs/{organization_id}/documents/{document_id}/versions", response_model=DocumentVersionsResponse)
@app.get("/api/orgs/{organization_id}/documents/{document_id}/versions", response_model=DocumentVersionsResponse)
async def list_document_versions(
    organization_id: str,
    document_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
):
    service = _require_docstore()
    org_context = _sync_authenticated_org(
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


@app.post("/api/admin/docstore/gc", response_model=DocstoreGcResponse)
async def run_docstore_gc(
    dry_run: bool = Query(default=True),
    max_delete: int = Query(default=100, ge=1, le=5000),
):
    service = _require_docstore()
    result = service.gc_unreferenced_objects(dry_run=dry_run, max_delete=max_delete)
    return DocstoreGcResponse(**result)


@app.get("/api/me", response_model=MeResponse)
async def me(auth: AuthContext = Depends(get_auth_context)):
    service = _require_docstore()
    context = _sync_authenticated_org(service, auth, create_if_missing=True)
    organization_id = context["organization_id"]
    organization_role = context["organization_role"]

    return MeResponse(
        user_id=auth.user_id,
        clerk_org_id=context["clerk_org_id"],
        clerk_org_slug=context["clerk_org_slug"],
        organization_id=organization_id,
        organization_role=organization_role,
        email=auth.email,
        first_name=auth.first_name,
        last_name=auth.last_name,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
