"""
Clerk authentication middleware and helpers for FastAPI.
"""

from dataclasses import dataclass
import os
import jwt
from jwt import PyJWKClient
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Paths that do not require authentication
PUBLIC_PATHS = [
    "/api/healthz",
    "/api/parse-legislation-mock",
    "/docs",
    "/redoc",
    "/openapi.json",
]


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    clerk_org_id: str | None = None
    clerk_org_slug: str | None = None
    clerk_org_role: str | None = None
    clerk_org_permissions: tuple[str, ...] = ()
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None


def _normalize_org_role(role: str | None) -> str | None:
    if not role:
        return None
    if role.startswith("org:"):
        return role
    return f"org:{role}"


def _normalize_org_permissions(raw_permissions: object) -> tuple[str, ...]:
    if raw_permissions is None:
        return ()
    if isinstance(raw_permissions, str):
        items = [item.strip() for item in raw_permissions.split(",") if item.strip()]
    elif isinstance(raw_permissions, list):
        items = [str(item).strip() for item in raw_permissions if str(item).strip()]
    else:
        return ()
    normalized: list[str] = []
    for item in items:
        normalized.append(item if item.startswith("org:") else f"org:{item}")
    return tuple(normalized)


def _extract_auth_context(clerk_user: dict) -> AuthContext:
    organization_claim = clerk_user.get("o")
    clerk_org_id = clerk_user.get("org_id")
    clerk_org_slug = clerk_user.get("org_slug")
    clerk_org_role = _normalize_org_role(clerk_user.get("org_role"))
    clerk_org_permissions = _normalize_org_permissions(clerk_user.get("org_permissions"))

    if isinstance(organization_claim, dict):
        clerk_org_id = organization_claim.get("id") or clerk_org_id
        clerk_org_slug = organization_claim.get("slg") or clerk_org_slug
        clerk_org_role = _normalize_org_role(organization_claim.get("rol")) or clerk_org_role
        claim_permissions = organization_claim.get("per")
        if claim_permissions is not None:
            clerk_org_permissions = _normalize_org_permissions(claim_permissions)

    return AuthContext(
        user_id=clerk_user["sub"],
        clerk_org_id=clerk_org_id,
        clerk_org_slug=clerk_org_slug,
        clerk_org_role=clerk_org_role,
        clerk_org_permissions=clerk_org_permissions,
        email=(
            clerk_user.get("email")
            or clerk_user.get("email_address")
            or clerk_user.get("primary_email_address")
        ),
        first_name=clerk_user.get("first_name") or clerk_user.get("given_name"),
        last_name=clerk_user.get("last_name") or clerk_user.get("family_name"),
    )


class ClerkAuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that verifies Clerk JWTs on every request
    except those whose path is in PUBLIC_PATHS.

    On success the decoded token payload is stored on
    ``request.state.clerk_user`` for downstream handlers.
    """

    def __init__(self, app):
        super().__init__(app)
        clerk_url = os.getenv("CLERK_FRONTEND_API_URL", "")
        if not clerk_url:
            raise RuntimeError(
                "CLERK_FRONTEND_API_URL environment variable is required"
            )
        jwks_url = f"{clerk_url.rstrip('/')}/.well-known/jwks.json"
        self._jwks_client = PyJWKClient(jwks_url, cache_keys=True)

    async def dispatch(self, request: Request, call_next):
        # Allow public endpoints through without auth
        if any(request.url.path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)

        # Extract Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[len("Bearer "):]

        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
        except jwt.ExpiredSignatureError:
            return JSONResponse(
                status_code=401, content={"detail": "Token has expired"}
            )
        except jwt.InvalidTokenError as exc:
            return JSONResponse(
                status_code=401, content={"detail": f"Invalid token: {exc}"}
            )

        # Attach decoded claims so handlers can use them
        request.state.clerk_user = payload
        return await call_next(request)


def get_current_user_id(request: Request) -> str:
    """
    Extract the Clerk user ID (``sub`` claim) from an authenticated request.

    Usage as a FastAPI dependency::

        @app.get("/api/me")
        async def me(user_id: str = Depends(get_current_user_id)):
            return {"user_id": user_id}
    """
    clerk_user = getattr(request.state, "clerk_user", None)
    if clerk_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = clerk_user.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=401, detail="User ID not found in token"
        )
    return user_id


def get_auth_context(request: Request) -> AuthContext:
    clerk_user = getattr(request.state, "clerk_user", None)
    if clerk_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = clerk_user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    return _extract_auth_context(clerk_user)
