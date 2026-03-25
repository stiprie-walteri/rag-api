from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import AuthContext, get_auth_context
from app.api.dependencies import require_docstore, sync_authenticated_org
from app.services.storage.service import (
    DocumentNotFoundError,
    DocumentAccessDeniedError,
)

router = APIRouter(tags=["Folders"])


class FolderItem(BaseModel):
    id: str
    organization_id: str
    name: str
    created_at: datetime
    created_by: str


class CreateFolderRequest(BaseModel):
    name: str


class RenameFolderRequest(BaseModel):
    name: str


class MoveDocumentRequest(BaseModel):
    folder_id: str | None = None


@router.post("/api/orgs/{organization_id}/folders", response_model=FolderItem, status_code=201)
async def create_folder(
    organization_id: str,
    body: CreateFolderRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    row = service.create_folder(
        organization_id=org_context["organization_id"],
        name=body.name,
        actor_user_id=auth.user_id,
    )
    return FolderItem(**row)


@router.get("/api/orgs/{organization_id}/folders", response_model=list[FolderItem])
async def list_folders(
    organization_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    rows = service.list_folders(
        organization_id=org_context["organization_id"],
        actor_user_id=auth.user_id,
    )
    return [FolderItem(**r) for r in rows]


@router.patch("/api/orgs/{organization_id}/folders/{folder_id}", status_code=204)
async def rename_folder(
    organization_id: str,
    folder_id: str,
    body: RenameFolderRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        service.rename_folder(
            folder_id=folder_id,
            organization_id=org_context["organization_id"],
            name=body.name,
            actor_user_id=auth.user_id,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/api/orgs/{organization_id}/folders/{folder_id}", status_code=204)
async def delete_folder(
    organization_id: str,
    folder_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        service.delete_folder(
            folder_id=folder_id,
            organization_id=org_context["organization_id"],
            actor_user_id=auth.user_id,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/api/orgs/{organization_id}/documents/{document_id}/folder", status_code=204)
async def move_document_to_folder(
    organization_id: str,
    document_id: str,
    body: MoveDocumentRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        service.move_document_to_folder(
            document_id=document_id,
            organization_id=org_context["organization_id"],
            folder_id=body.folder_id,
            actor_user_id=auth.user_id,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
