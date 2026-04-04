from fastapi import APIRouter, Depends
from pydantic import BaseModel
from app.core.auth import AuthContext, get_auth_context
from app.api.dependencies import require_docstore, sync_authenticated_org

router = APIRouter(tags=["User & Health"])

class HealthResponse(BaseModel):
    status: str
    message: str

class MeResponse(BaseModel):
    user_id: str
    clerk_org_id: str | None = None
    clerk_org_slug: str | None = None
    organization_id: str | None = None
    organization_role: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None

@router.get("/api/healthz", response_model=HealthResponse)
async def health_check():
    return HealthResponse(
        status="healthy",
        message="API is running successfully",
    )

@router.get("/api/me", response_model=MeResponse)
async def me(auth: AuthContext = Depends(get_auth_context)):
    service = require_docstore()
    context = await sync_authenticated_org(service, auth, create_if_missing=True)
    return MeResponse(
        user_id=auth.user_id,
        clerk_org_id=context["clerk_org_id"],
        clerk_org_slug=context["clerk_org_slug"],
        organization_id=context["organization_id"],
        organization_role=context["organization_role"],
        email=auth.email,
        first_name=auth.first_name,
        last_name=auth.last_name,
    )
