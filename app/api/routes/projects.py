from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.dependencies import require_docstore, sync_authenticated_org
from app.api.routes.documents import DocumentListResponse, _to_document_list_item
from app.core.auth import AuthContext, get_auth_context
from app.services.storage.service import (
    DocumentAccessDeniedError,
    DocumentNotFoundError,
    DocumentOperationConflictError,
    OrganizationMismatchError,
    ProjectNotFoundError,
)

router = APIRouter(tags=["Projects"])


class ProjectItem(BaseModel):
    project_id: str
    organization_id: str
    name: str
    description: str | None = None
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str
    document_count: int = 0


class CreateProjectRequest(BaseModel):
    name: str
    description: str | None = None


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class MoveDocumentToProjectRequest(BaseModel):
    project_id: str


@router.post("/api/orgs/{organization_id}/projects", response_model=ProjectItem, status_code=201)
async def create_project(
    organization_id: str,
    body: CreateProjectRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        row = service.create_project(
            organization_id=org_context["organization_id"],
            name=body.name,
            description=body.description,
            actor_user_id=auth.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ProjectItem(**row)


@router.get("/api/orgs/{organization_id}/projects", response_model=list[ProjectItem])
async def list_projects(
    organization_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    rows = service.list_projects(
        organization_id=org_context["organization_id"],
        actor_user_id=auth.user_id,
    )
    return [ProjectItem(**row) for row in rows]


@router.get("/api/orgs/{organization_id}/projects/{project_id}", response_model=ProjectItem)
async def get_project(
    organization_id: str,
    project_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        row = service.get_project(
            organization_id=org_context["organization_id"],
            project_id=project_id,
            actor_user_id=auth.user_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return ProjectItem(**row)


@router.patch("/api/orgs/{organization_id}/projects/{project_id}", response_model=ProjectItem)
async def update_project(
    organization_id: str,
    project_id: str,
    body: UpdateProjectRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        row = service.update_project(
            organization_id=org_context["organization_id"],
            project_id=project_id,
            actor_user_id=auth.user_id,
            name=body.name,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return ProjectItem(**row)


@router.delete("/api/orgs/{organization_id}/projects/{project_id}", status_code=204)
async def delete_project(
    organization_id: str,
    project_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        service.delete_project(
            organization_id=org_context["organization_id"],
            project_id=project_id,
            actor_user_id=auth.user_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DocumentOperationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get(
    "/api/orgs/{organization_id}/projects/{project_id}/documents",
    response_model=DocumentListResponse,
)
async def list_project_documents(
    organization_id: str,
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        items_raw = service.list_documents(
            organization_id=org_context["organization_id"],
            actor_user_id=auth.user_id,
            limit=limit,
            offset=offset,
            project_id=project_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    items = [_to_document_list_item(item) for item in items_raw]
    next_offset = offset + len(items) if len(items) == limit else None
    return DocumentListResponse(items=items, limit=limit, offset=offset, next_offset=next_offset)


@router.patch("/api/orgs/{organization_id}/documents/{document_id}/project", status_code=204)
async def move_document_to_project(
    organization_id: str,
    document_id: str,
    body: MoveDocumentToProjectRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        service.move_document_to_project(
            document_id=document_id,
            organization_id=org_context["organization_id"],
            project_id=body.project_id,
            actor_user_id=auth.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (DocumentNotFoundError, ProjectNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (DocumentAccessDeniedError, OrganizationMismatchError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
