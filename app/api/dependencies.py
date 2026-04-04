import os
import logging
from fastapi import HTTPException
from app.services.storage.service import (
    DocumentStorageSettings,
    DocumentStorageService,
    OrganizationMismatchError,
    OrganizationNotFoundError,
    DocumentAccessDeniedError,
)
from app.core.auth import AuthContext

logger = logging.getLogger(__name__)

docstore_service: DocumentStorageService | None = None
try:
    docstore_service = DocumentStorageService(DocumentStorageSettings.from_env())
except ValueError as exc:
    logger.warning("Persistent document storage is disabled: %s", exc)

def require_docstore() -> DocumentStorageService:
    if docstore_service is None:
        raise HTTPException(
            status_code=503,
            detail="Persistent document storage is not configured. Set POSTGRES_DSN and MinIO env vars.",
        )
    return docstore_service

async def sync_authenticated_org(
    service: DocumentStorageService,
    auth: AuthContext,
    *,
    requested_organization_id: str | None = None,
    create_if_missing: bool,
) -> dict:
    try:
        context = await service.sync_authenticated_user(
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
