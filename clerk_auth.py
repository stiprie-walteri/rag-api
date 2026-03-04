"""
Clerk authentication middleware and helpers for FastAPI.
"""

import os
import jwt
import httpx
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
