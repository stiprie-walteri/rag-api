import asyncio
import hashlib
import io
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from minio import Minio
from minio.error import S3Error
from psycopg.rows import dict_row
from psycopg.types.json import Json


logger = logging.getLogger(__name__)

DOCUMENT_MEMBER_ROLES = {"owner", "editor", "viewer"}
DOCUMENT_WRITE_ROLES = {"owner", "editor"}
DOCUMENT_READ_ROLES = {"owner", "editor", "viewer"}
ORGANIZATION_MEMBER_ROLES = {"owner", "member"}


class DocumentStorageError(Exception):
    pass


class DocumentNotFoundError(DocumentStorageError):
    pass


class OrganizationMismatchError(DocumentStorageError):
    pass


class OrganizationNotFoundError(DocumentStorageError):
    pass


class DocumentVersionNotFoundError(DocumentStorageError):
    pass


class DocumentHasNoVersionsError(DocumentStorageError):
    pass


class DocumentAccessDeniedError(DocumentStorageError):
    pass


class DocumentMemberNotFoundError(DocumentStorageError):
    pass


class DocumentOperationConflictError(DocumentStorageError):
    pass


class ProjectNotFoundError(DocumentStorageError):
    pass


def _normalize_legislation_template_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_sql_statements(sql_script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []

    for line in sql_script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(current).strip()
            if statement:
                statements.append(statement)
            current = []

    trailing = "\n".join(current).strip()
    if trailing:
        statements.append(trailing)
    return statements


@dataclass(frozen=True)
class DocumentStorageSettings:
    postgres_dsn: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_secure: bool
    minio_bucket: str
    migrations_dir: Path

    @classmethod
    def from_env(cls) -> "DocumentStorageSettings":
        postgres_dsn = os.getenv("POSTGRES_DSN", "").strip()
        if not postgres_dsn:
            raise ValueError("POSTGRES_DSN must be set to enable persistent document storage.")

        minio_endpoint = os.getenv("MINIO_ENDPOINT", "localhost:9000")
        minio_access_key = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
        minio_secret_key = os.getenv("MINIO_SECRET_KEY", "minioadmin")
        minio_secure = _parse_bool(os.getenv("MINIO_SECURE"), default=False)
        minio_bucket = os.getenv("MINIO_BUCKET", "docstore")
        migrations_dir = Path(os.getenv("MIGRATIONS_DIR", "migrations"))

        return cls(
            postgres_dsn=postgres_dsn,
            minio_endpoint=minio_endpoint,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
            minio_secure=minio_secure,
            minio_bucket=minio_bucket,
            migrations_dir=migrations_dir,
        )


class DocumentStorageService:
    def __init__(self, settings: DocumentStorageSettings):
        self.settings = settings
        self.minio_client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    @staticmethod
    def compute_hash(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    # ------------------------------------------------------------------
    # Startup helpers — remain synchronous (called once at app startup)
    # ------------------------------------------------------------------

    def run_migrations(self) -> None:
        migrations_dir = self.settings.migrations_dir
        if not migrations_dir.exists():
            logger.warning("Migration directory %s does not exist. Skipping migrations.", migrations_dir)
            return

        migration_files = sorted(migrations_dir.glob("*.sql"))
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )

            for migration_file in migration_files:
                version = migration_file.name
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s;", (version,))
                    if cur.fetchone():
                        continue

                script = migration_file.read_text(encoding="utf-8")
                statements = _split_sql_statements(script)
                if not statements:
                    continue

                logger.info("Applying migration %s", version)
                with conn.transaction():
                    with conn.cursor() as cur:
                        for statement in statements:
                            cur.execute(statement)
                        cur.execute("INSERT INTO schema_migrations (version) VALUES (%s);", (version,))

    def ensure_bucket(self) -> None:
        if self.minio_client.bucket_exists(self.settings.minio_bucket):
            return
        self.minio_client.make_bucket(self.settings.minio_bucket)
        logger.info("Created MinIO bucket %s", self.settings.minio_bucket)

    def initialize(self) -> None:
        self.run_migrations()
        self.ensure_bucket()

    # ------------------------------------------------------------------
    # MinIO helpers — sync MinIO SDK wrapped in asyncio.to_thread
    # ------------------------------------------------------------------

    async def minio_put_if_missing(self, bucket: str, key: str, payload: bytes) -> bool:
        def _put() -> bool:
            try:
                self.minio_client.stat_object(bucket, key)
                return False
            except S3Error as exc:
                if exc.code not in {"NoSuchKey", "NoSuchObject"}:
                    raise
            self.minio_client.put_object(
                bucket_name=bucket,
                object_name=key,
                data=io.BytesIO(payload),
                length=len(payload),
                content_type="text/markdown; charset=utf-8",
            )
            return True

        return await asyncio.to_thread(_put)

    async def minio_get(self, bucket: str, key: str) -> bytes:
        def _get() -> bytes:
            response = self.minio_client.get_object(bucket_name=bucket, object_name=key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_get)

    # ------------------------------------------------------------------
    # Cursor-level helpers — async, reuse an existing AsyncCursor
    # ------------------------------------------------------------------

    async def _ensure_user(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        user_id: str,
        primary_email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        await cur.execute(
            """
            INSERT INTO users (
                id,
                primary_email,
                first_name,
                last_name,
                created_at,
                updated_at,
                last_seen_at
            )
            VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW())
            ON CONFLICT (id) DO UPDATE
            SET primary_email = COALESCE(EXCLUDED.primary_email, users.primary_email),
                first_name = COALESCE(EXCLUDED.first_name, users.first_name),
                last_name = COALESCE(EXCLUDED.last_name, users.last_name),
                updated_at = NOW(),
                last_seen_at = NOW();
            """,
            (user_id, primary_email, first_name, last_name),
        )

    async def _get_organization_by_id(
        self,
        cur: psycopg.AsyncCursor[Any],
        organization_id: str,
    ) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT id, clerk_org_id, clerk_org_slug, name, created_at
            FROM organizations
            WHERE id = %s;
            """,
            (organization_id,),
        )
        return await cur.fetchone()

    async def _get_organization_by_clerk_org_id(
        self,
        cur: psycopg.AsyncCursor[Any],
        clerk_org_id: str | None = None,
    ) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT id, clerk_org_id, clerk_org_slug, name, created_at
            FROM organizations
            WHERE clerk_org_id = %s;
            """,
            (clerk_org_id,),
        )
        return await cur.fetchone()

    async def _upsert_organization_membership(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        user_id: str,
        role: str,
        actor_user_id: str,
        overwrite_role: bool,
    ) -> None:
        if role not in ORGANIZATION_MEMBER_ROLES:
            raise ValueError(f"Unsupported organization role: {role}")

        if overwrite_role:
            await cur.execute(
                """
                INSERT INTO organization_memberships (
                    organization_id,
                    user_id,
                    role,
                    created_at,
                    created_by,
                    updated_at,
                    updated_by
                )
                VALUES (%s, %s, %s, NOW(), %s, NOW(), %s)
                ON CONFLICT (organization_id, user_id) DO UPDATE
                SET role = EXCLUDED.role,
                    updated_at = NOW(),
                    updated_by = EXCLUDED.updated_by;
                """,
                (organization_id, user_id, role, actor_user_id, actor_user_id),
            )
            return

        await cur.execute(
            """
            INSERT INTO organization_memberships (
                organization_id,
                user_id,
                role,
                created_at,
                created_by,
                updated_at,
                updated_by
            )
            VALUES (%s, %s, %s, NOW(), %s, NOW(), %s)
            ON CONFLICT (organization_id, user_id) DO UPDATE
            SET updated_at = NOW(),
                updated_by = EXCLUDED.updated_by,
                role = CASE
                    WHEN organization_memberships.role = 'owner' THEN organization_memberships.role
                    ELSE EXCLUDED.role
                END;
            """,
            (organization_id, user_id, role, actor_user_id, actor_user_id),
        )

    async def _get_organization_membership_role(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        user_id: str,
    ) -> str | None:
        await cur.execute(
            """
            SELECT role
            FROM organization_memberships
            WHERE organization_id = %s
              AND user_id = %s;
            """,
            (organization_id, user_id),
        )
        row = await cur.fetchone()
        return row["role"] if row else None

    async def _ensure_user_in_organization(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        user_id: str,
    ) -> str:
        REQUIRE_ORG_VALIDATION = os.getenv("REQUIRE_ORG_VALIDATION", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not REQUIRE_ORG_VALIDATION:
            return "owner"

        role = await self._get_organization_membership_role(cur, organization_id=organization_id, user_id=user_id)
        if role is None:
            raise DocumentAccessDeniedError(
                f"User {user_id} is not a member of organization {organization_id}."
            )
        return role

    async def _upsert_document_membership(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        document_id: str,
        user_id: str,
        role: str,
        actor_user_id: str,
    ) -> None:
        if role not in DOCUMENT_MEMBER_ROLES:
            raise ValueError(f"Unsupported document role: {role}")

        await cur.execute(
            """
            INSERT INTO document_memberships (
                organization_id,
                document_id,
                user_id,
                role,
                assigned_at,
                assigned_by
            )
            VALUES (%s, %s, %s, %s, NOW(), %s)
            ON CONFLICT (organization_id, document_id, user_id) DO UPDATE
            SET role = EXCLUDED.role,
                assigned_at = NOW(),
                assigned_by = EXCLUDED.assigned_by;
            """,
            (organization_id, document_id, user_id, role, actor_user_id),
        )

    async def _get_document_membership_role(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        document_id: str,
        user_id: str,
    ) -> str | None:
        await cur.execute(
            """
            SELECT role
            FROM document_memberships
            WHERE organization_id = %s
              AND document_id = %s
              AND user_id = %s;
            """,
            (organization_id, document_id, user_id),
        )
        row = await cur.fetchone()
        return row["role"] if row else None

    async def _count_document_owners(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        document_id: str,
    ) -> int:
        await cur.execute(
            """
            SELECT COUNT(*) AS owner_count
            FROM document_memberships
            WHERE organization_id = %s
              AND document_id = %s
              AND role = 'owner';
            """,
            (organization_id, document_id),
        )
        row = await cur.fetchone()
        return int(row["owner_count"]) if row else 0

    async def _write_audit(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        actor_user_id: str,
        action: str,
        object_type: str,
        object_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await cur.execute(
            """
            INSERT INTO audit_log (
                organization_id,
                actor_user_id,
                action,
                object_type,
                object_id,
                at,
                metadata_json
            )
            VALUES (%s, %s, %s, %s, %s, NOW(), %s);
            """,
            (
                organization_id,
                actor_user_id,
                action,
                object_type,
                object_id,
                Json(metadata) if metadata is not None else None,
            ),
        )

    async def _get_document_for_org(
        self,
        cur: psycopg.AsyncCursor[Any],
        organization_id: str,
        document_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        lock_clause = " FOR UPDATE" if for_update else ""
        await cur.execute(
            f"""
            SELECT
                d.id,
                d.organization_id,
                d.title,
                d.folder_id AS project_id,
                d.current_version_id,
                d.created_at,
                d.created_by,
                d.updated_at,
                f.name AS project_name,
                f.description AS project_description,
                f.legislation_template_ids,
                f.created_at AS project_created_at,
                f.created_by AS project_created_by,
                f.updated_at AS project_updated_at,
                f.updated_by AS project_updated_by
            FROM documents d
            LEFT JOIN folders f
                   ON f.id = d.folder_id
                  AND f.organization_id = d.organization_id
            WHERE d.id = %s
              AND d.organization_id = %s{lock_clause};
            """,
            (document_id, organization_id),
        )
        return await cur.fetchone()

    async def _get_project_for_org(
        self,
        cur: psycopg.AsyncCursor[Any],
        organization_id: str,
        project_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        lock_clause = " FOR UPDATE" if for_update else ""
        await cur.execute(
            f"""
            SELECT
                f.id,
                f.organization_id,
                f.name,
                f.description AS description,
                f.legislation_template_ids,
                f.compliance_result,
                f.created_at,
                f.created_by,
                f.updated_at,
                f.updated_by,
                (
                    SELECT COUNT(*)::INT
                    FROM documents d
                    WHERE d.folder_id = f.id
                      AND d.organization_id = f.organization_id
                ) AS document_count
            FROM folders f
            WHERE f.id = %s
              AND f.organization_id = %s{lock_clause};
            """,
            (project_id, organization_id),
        )
        return await cur.fetchone()

    async def _assert_project_in_org(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        project_id: str,
        for_update: bool = False,
    ) -> dict[str, Any]:
        row = await self._get_project_for_org(
            cur,
            organization_id=organization_id,
            project_id=project_id,
            for_update=for_update,
        )
        if row is not None:
            return row

        await cur.execute("SELECT organization_id FROM folders WHERE id = %s;", (project_id,))
        existing = await cur.fetchone()
        if existing is None:
            raise ProjectNotFoundError(f"Project {project_id} was not found.")
        raise OrganizationMismatchError(
            f"Project {project_id} belongs to organization {existing['organization_id']}, not {organization_id}."
        )

    async def _assert_document_in_org(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        document_id: str,
        for_update: bool = False,
    ) -> dict[str, Any]:
        row = await self._get_document_for_org(
            cur,
            organization_id=organization_id,
            document_id=document_id,
            for_update=for_update,
        )
        if row is not None:
            return row

        await cur.execute("SELECT organization_id FROM documents WHERE id = %s;", (document_id,))
        existing = await cur.fetchone()
        if existing is None:
            raise DocumentNotFoundError(f"Document {document_id} was not found.")
        raise OrganizationMismatchError(
            f"Document {document_id} belongs to organization {existing['organization_id']}, not {organization_id}."
        )

    async def _assert_document_access(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        document_id: str,
        user_id: str,
        allowed_roles: set[str],
        for_update: bool = False,
    ) -> tuple[dict[str, Any], str]:
        organization_role = await self._ensure_user_in_organization(
            cur,
            organization_id=organization_id,
            user_id=user_id,
        )

        REQUIRE_ORG_VALIDATION = os.getenv("REQUIRE_ORG_VALIDATION", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not REQUIRE_ORG_VALIDATION:
            await cur.execute("SELECT * FROM documents WHERE id = %s;", (document_id,))
            doc_row = await cur.fetchone()
            if doc_row is None:
                raise DocumentNotFoundError(f"Document {document_id} was not found.")
            return doc_row, "owner"

        document_row = await self._assert_document_in_org(
            cur,
            organization_id=organization_id,
            document_id=document_id,
            for_update=for_update,
        )

        if organization_role == "owner":
            return document_row, "owner"

        document_role = await self._get_document_membership_role(
            cur,
            organization_id=organization_id,
            document_id=document_id,
            user_id=user_id,
        )
        if document_role in allowed_roles:
            return document_row, document_role

        raise DocumentAccessDeniedError(
            f"User {user_id} does not have access to document {document_id}."
        )

    def _map_document_row(self, row: dict[str, Any], *, my_role: str | None = None) -> dict[str, Any]:
        return {
            "document_id": row["id"],
            "organization_id": row["organization_id"],
            "title": row["title"],
            "project_id": str(row["project_id"]) if row.get("project_id") is not None else None,
            "project": (
                {
                    "project_id": str(row["project_id"]),
                    "organization_id": row["organization_id"],
                    "name": row.get("project_name"),
                    "description": row.get("project_description"),
                    "legislation_template_ids": _normalize_legislation_template_ids(
                        row.get("legislation_template_ids")
                    ),
                    "created_at": row.get("project_created_at"),
                    "created_by": row.get("project_created_by"),
                    "updated_at": row.get("project_updated_at"),
                    "updated_by": row.get("project_updated_by"),
                }
                if row.get("project_id") is not None and row.get("project_name") is not None
                else None
            ),
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "updated_at": row["updated_at"],
            "current_version_id": row["current_version_id"],
            "my_role": my_role,
        }

    @staticmethod
    def _map_project_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "project_id": str(row["id"]),
            "organization_id": row["organization_id"],
            "name": row["name"],
            "description": row.get("description"),
            "legislation_template_ids": _normalize_legislation_template_ids(
                row.get("legislation_template_ids")
            ),
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
            "document_count": int(row.get("document_count") or 0),
        }

    @staticmethod
    def _map_version_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "version_id": row["id"],
            "organization_id": row["organization_id"],
            "document_id": row["document_id"],
            "version_no": row["version_no"],
            "content_hash": row["content_hash"],
            "object_key": row["object_key"],
            "size_bytes": row["size_bytes"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "message": row["message"],
            "parent_version_id": row["parent_version_id"],
            "compliance_result": row.get("compliance_result"),
        }

    @staticmethod
    def _map_document_member_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": row["user_id"],
            "role": row["role"],
            "primary_email": row["primary_email"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "assigned_at": row["assigned_at"],
            "assigned_by": row["assigned_by"],
        }

    # ------------------------------------------------------------------
    # Public async methods
    # ------------------------------------------------------------------

    async def sync_authenticated_user(
        self,
        *,
        clerk_user_id: str,
        clerk_org_id: str | None = None,
        clerk_org_slug: str | None = None,
        clerk_org_role: str | None = None,
        primary_email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        requested_organization_id: str | None = None,
        create_if_missing: bool = True,
    ) -> dict[str, Any]:
        if not clerk_user_id:
            raise ValueError("clerk_user_id is required.")

        if not clerk_org_id:
            raise DocumentAccessDeniedError(
                "No organization active. You must be added to an organization to access this application."
            )

        # Map Clerk role to our DB role ("owner" or "member")
        local_org_role = "owner" if clerk_org_role and "admin" in clerk_org_role else "member"
        organization_name = clerk_org_slug or clerk_org_id

        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._ensure_user(
                        cur,
                        user_id=clerk_user_id,
                        primary_email=primary_email,
                        first_name=first_name,
                        last_name=last_name,
                    )

                    organization_row = await self._get_organization_by_clerk_org_id(cur, clerk_org_id)

                    if organization_row is None:
                        if not create_if_missing:
                            raise OrganizationNotFoundError(
                                f"Organization {clerk_org_id} was not found."
                            )
                        organization_id = str(uuid.uuid4())
                        await cur.execute(
                            """
                            INSERT INTO organizations (
                                id,
                                clerk_org_id,
                                clerk_org_slug,
                                name,
                                created_at
                            )
                            VALUES (%s, %s, %s, %s, NOW());
                            """,
                            (organization_id, clerk_org_id, clerk_org_slug, organization_name),
                        )
                        organization_row = await self._get_organization_by_id(cur, organization_id)
                    else:
                        # Keep slug and name in sync with Clerk
                        await cur.execute(
                            """
                            UPDATE organizations
                            SET clerk_org_slug = %s,
                                name = COALESCE(%s, name)
                            WHERE id = %s;
                            """,
                            (clerk_org_slug, organization_name, organization_row["id"]),
                        )
                        organization_row = await self._get_organization_by_id(cur, organization_row["id"])

                    await self._upsert_organization_membership(
                        cur,
                        organization_id=organization_row["id"],
                        user_id=clerk_user_id,
                        role=local_org_role,
                        actor_user_id=clerk_user_id,
                        overwrite_role=True,
                    )

        return {
            "organization_id": organization_row["id"],
            "clerk_org_id": clerk_org_id,
            "clerk_org_slug": clerk_org_slug,
            "organization_role": local_org_role,
            "user_id": clerk_user_id,
        }

    async def create_version(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
        markdown_bytes: bytes,
        document_id: str | None = None,
        project_id: str | None = None,
        title: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        if not markdown_bytes:
            raise ValueError("Document content cannot be empty.")

        content_hash = self.compute_hash(markdown_bytes)
        object_key = f"objects/{content_hash}.md"
        size_bytes = len(markdown_bytes)
        uploaded_object = await self.minio_put_if_missing(self.settings.minio_bucket, object_key, markdown_bytes)

        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._ensure_user(cur, user_id=actor_user_id)
                    await self._ensure_user_in_organization(
                        cur,
                        organization_id=organization_id,
                        user_id=actor_user_id,
                    )

                    if document_id is None:
                        if project_id:
                            await self._assert_project_in_org(
                                cur,
                                organization_id=organization_id,
                                project_id=project_id,
                            )
                        else:
                            # Auto-assign to a "General" project, creating one if needed
                            await cur.execute(
                                """
                                SELECT id FROM folders
                                WHERE organization_id = %s AND name = 'General'
                                LIMIT 1;
                                """,
                                (organization_id,),
                            )
                            row = await cur.fetchone()
                            if row:
                                project_id = str(row["id"])
                            else:
                                project_id = str(uuid.uuid4())
                                await cur.execute(
                                    """
                                    INSERT INTO folders (
                                        id, organization_id, name, description,
                                        created_at, created_by, updated_at, updated_by
                                    ) VALUES (%s, %s, 'General',
                                        'Auto-created default project for uploads without a project.',
                                        NOW(), %s, NOW(), %s);
                                    """,
                                    (project_id, organization_id, actor_user_id, actor_user_id),
                                )
                        document_id = str(uuid.uuid4())
                        parent_version_id = None
                        version_no = 1
                        await cur.execute(
                            """
                            INSERT INTO documents (
                                id,
                                organization_id,
                                title,
                                folder_id,
                                current_version_id,
                                created_at,
                                created_by,
                                updated_at
                            )
                            VALUES (%s, %s, %s, %s, NULL, NOW(), %s, NOW());
                            """,
                            (document_id, organization_id, title, project_id, actor_user_id),
                        )
                        await self._upsert_document_membership(
                            cur,
                            organization_id=organization_id,
                            document_id=document_id,
                            user_id=actor_user_id,
                            role="owner",
                            actor_user_id=actor_user_id,
                        )
                    else:
                        document_row, _ = await self._assert_document_access(
                            cur,
                            organization_id=organization_id,
                            document_id=document_id,
                            user_id=actor_user_id,
                            allowed_roles=DOCUMENT_WRITE_ROLES,
                            for_update=True,
                        )
                        parent_version_id = document_row["current_version_id"]
                        active_project_id = (
                            str(document_row["project_id"])
                            if document_row.get("project_id") is not None
                            else None
                        )

                        if project_id is not None:
                            await self._assert_project_in_org(
                                cur,
                                organization_id=organization_id,
                                project_id=project_id,
                            )
                            if active_project_id != project_id:
                                await cur.execute(
                                    """
                                    UPDATE documents
                                    SET folder_id = %s
                                    WHERE id = %s
                                      AND organization_id = %s;
                                    """,
                                    (project_id, document_id, organization_id),
                                )
                        else:
                            project_id = active_project_id

                        if parent_version_id is None:
                            version_no = 1
                        else:
                            await cur.execute(
                                """
                                SELECT version_no
                                FROM document_versions
                                WHERE id = %s
                                  AND organization_id = %s
                                  AND document_id = %s;
                                """,
                                (parent_version_id, organization_id, document_id),
                            )
                            parent_version_row = await cur.fetchone()
                            if parent_version_row is None:
                                raise DocumentVersionNotFoundError(
                                    f"Current version for document {document_id} was not found."
                                )
                            version_no = parent_version_row["version_no"] + 1

                        if title is not None:
                            await cur.execute(
                                """
                                UPDATE documents
                                SET title = %s
                                WHERE id = %s
                                  AND organization_id = %s;
                                """,
                                (title, document_id, organization_id),
                            )

                    version_id = str(uuid.uuid4())
                    await cur.execute(
                        """
                        INSERT INTO document_versions (
                            id,
                            organization_id,
                            document_id,
                            version_no,
                            content_hash,
                            object_key,
                            size_bytes,
                            created_at,
                            created_by,
                            message,
                            parent_version_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s);
                        """,
                        (
                            version_id,
                            organization_id,
                            document_id,
                            version_no,
                            content_hash,
                            object_key,
                            size_bytes,
                            actor_user_id,
                            message,
                            parent_version_id,
                        ),
                    )

                    await cur.execute(
                        """
                        UPDATE documents
                        SET current_version_id = %s,
                            updated_at = NOW()
                        WHERE id = %s
                          AND organization_id = %s;
                        """,
                        (version_id, document_id, organization_id),
                    )

                    audit_metadata = {
                        "document_id": document_id,
                        "project_id": project_id,
                        "version_id": version_id,
                        "version_no": version_no,
                        "content_hash": content_hash,
                        "size_bytes": size_bytes,
                        "object_key": object_key,
                        "object_uploaded": uploaded_object,
                    }

                    await self._write_audit(
                        cur,
                        organization_id=organization_id,
                        actor_user_id=actor_user_id,
                        action="document.upload",
                        object_type="document",
                        object_id=document_id,
                        metadata=audit_metadata,
                    )
                    await self._write_audit(
                        cur,
                        organization_id=organization_id,
                        actor_user_id=actor_user_id,
                        action="version.create",
                        object_type="document",
                        object_id=document_id,
                        metadata=audit_metadata,
                    )

        logger.info(
            "Uploaded markdown: org=%s doc=%s version=%s size=%s hash=%s uploaded_object=%s user=%s",
            organization_id,
            document_id,
            version_no,
            size_bytes,
            content_hash,
            uploaded_object,
            actor_user_id,
        )

        return {
            "document_id": document_id,
            "project_id": project_id,
            "version_id": version_id,
            "version_no": version_no,
            "content_hash": content_hash,
            "object_key": object_key,
            "size_bytes": size_bytes,
        }

    async def save_document_chunks(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        chunks: list[dict],
    ) -> None:
        if not chunks:
            return

        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    for chunk in chunks:
                        chunk_id = str(uuid.uuid4())
                        await cur.execute(
                            """
                            INSERT INTO document_chunks (
                                id,
                                organization_id,
                                document_id,
                                version_id,
                                chunk_level,
                                title,
                                start_page,
                                end_page,
                                text_content,
                                created_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW());
                            """,
                            (
                                chunk_id,
                                organization_id,
                                document_id,
                                version_id,
                                chunk.get("level", 1),
                                chunk.get("title", ""),
                                chunk.get("start_page", 1),
                                chunk.get("end_page", 1),
                                chunk.get("text", ""),
                            ),
                        )

    async def get_document_chunks(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        actor_user_id: str,
        title: str | None = None,
        chunk_level: int | None = None,
    ) -> list[dict]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles=DOCUMENT_READ_ROLES,
                )

                query = """
                    SELECT
                        id,
                        organization_id,
                        document_id,
                        version_id,
                        chunk_level,
                        title,
                        start_page,
                        end_page,
                        text_content,
                        created_at
                    FROM document_chunks
                    WHERE organization_id = %s
                      AND document_id = %s
                      AND version_id = %s
                """
                params = [organization_id, document_id, version_id]

                if title is not None:
                    query += " AND title = %s"
                    params.append(title)

                if chunk_level is not None:
                    query += " AND chunk_level = %s"
                    params.append(chunk_level)

                query += " ORDER BY created_at ASC;"

                await cur.execute(query, tuple(params))
                rows = await cur.fetchall()

        return [
            {**row, "id": str(row["id"])}
            for row in rows
        ]

    async def delete_document(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str,
    ) -> dict:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles={"owner"},
                )

                await cur.execute(
                    """
                    SELECT object_key FROM document_versions
                    WHERE document_id = %s AND organization_id = %s;
                    """,
                    (document_id, organization_id),
                )
                version_rows = await cur.fetchall()
                object_keys = [r["object_key"] for r in version_rows if r.get("object_key")]

                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM document_chunks WHERE document_id = %s AND organization_id = %s;",
                    (document_id, organization_id),
                )
                chunk_count = (await cur.fetchone() or {}).get("cnt", 0)

                await cur.execute(
                    "DELETE FROM document_chunks WHERE document_id = %s AND organization_id = %s;",
                    (document_id, organization_id),
                )

                await cur.execute(
                    "DELETE FROM document_versions WHERE document_id = %s AND organization_id = %s;",
                    (document_id, organization_id),
                )

                await cur.execute(
                    "DELETE FROM documents WHERE id = %s AND organization_id = %s;",
                    (document_id, organization_id),
                )

                await conn.commit()

        def _delete_objects() -> tuple[list[str], list[str]]:
            deleted: list[str] = []
            failed: list[str] = []
            for key in object_keys:
                try:
                    self.minio_client.remove_object(self.settings.minio_bucket, key)
                    deleted.append(key)
                except S3Error as exc:
                    logger.warning("Failed to delete MinIO object %s: %s", key, exc)
                    failed.append(key)
            return deleted, failed

        deleted_objects, failed_objects = await asyncio.to_thread(_delete_objects)

        return {
            "document_id": document_id,
            "organization_id": organization_id,
            "chunks_deleted": chunk_count,
            "versions_deleted": len(version_rows),
            "objects_deleted": len(deleted_objects),
            "objects_failed": len(failed_objects),
        }

    async def list_documents(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
        limit: int,
        offset: int,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                organization_role = await self._ensure_user_in_organization(
                    cur,
                    organization_id=organization_id,
                    user_id=actor_user_id,
                )
                if project_id is not None:
                    await self._assert_project_in_org(
                        cur,
                        organization_id=organization_id,
                        project_id=project_id,
                    )
                project_filter = ""
                project_params: tuple = ()
                if project_id is not None:
                    project_filter = "AND d.folder_id = %s"
                    project_params = (project_id,)

                await cur.execute(
                    f"""
                    SELECT
                        d.id AS document_id,
                        d.organization_id,
                        d.title,
                        d.folder_id AS project_id,
                        d.created_at AS document_created_at,
                        d.created_by AS document_created_by,
                        d.updated_at AS document_updated_at,
                        d.current_version_id,
                        f.name AS project_name,
                        f.description AS project_description,
                        f.legislation_template_ids,
                        f.created_at AS project_created_at,
                        f.created_by AS project_created_by,
                        f.updated_at AS project_updated_at,
                        f.updated_by AS project_updated_by,
                        v.id AS version_id,
                        v.version_no,
                        v.content_hash,
                        v.object_key,
                        v.size_bytes,
                        v.created_at AS version_created_at,
                        v.created_by AS version_created_by,
                        v.message,
                        v.parent_version_id,
                        v.compliance_result,
                        CASE
                            WHEN %s = 'owner' THEN 'owner'
                            ELSE dm.role
                        END AS my_role
                    FROM documents d
                    LEFT JOIN document_memberships dm
                           ON dm.organization_id = d.organization_id
                          AND dm.document_id = d.id
                          AND dm.user_id = %s
                    LEFT JOIN document_versions v
                           ON v.id = d.current_version_id
                          AND v.organization_id = d.organization_id
                    LEFT JOIN folders f
                           ON f.id = d.folder_id
                          AND f.organization_id = d.organization_id
                    WHERE d.organization_id = %s
                      AND (%s = 'owner' OR dm.user_id IS NOT NULL)
                      {project_filter}
                    ORDER BY d.updated_at DESC
                    LIMIT %s
                    OFFSET %s;
                    """,
                    (
                        organization_role,
                        actor_user_id,
                        organization_id,
                        organization_role,
                        *project_params,
                        limit,
                        offset,
                    ),
                )
                rows = await cur.fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            current_version: dict[str, Any] | None = None
            if row["version_id"] is not None:
                current_version = {
                    "version_id": row["version_id"],
                    "organization_id": row["organization_id"],
                    "document_id": row["document_id"],
                    "version_no": row["version_no"],
                    "content_hash": row["content_hash"],
                    "object_key": row["object_key"],
                    "size_bytes": row["size_bytes"],
                    "created_at": row["version_created_at"],
                    "created_by": row["version_created_by"],
                    "message": row["message"],
                    "parent_version_id": row["parent_version_id"],
                    "compliance_result": row.get("compliance_result"),
                }

            items.append(
                {
                    "document_id": row["document_id"],
                    "organization_id": row["organization_id"],
                    "title": row["title"],
                    "project_id": str(row["project_id"]) if row.get("project_id") is not None else None,
                    "project": (
                        {
                            "project_id": str(row["project_id"]),
                            "organization_id": row["organization_id"],
                            "name": row.get("project_name"),
                            "description": row.get("project_description"),
                            "legislation_template_ids": _normalize_legislation_template_ids(
                                row.get("legislation_template_ids")
                            ),
                            "created_at": row.get("project_created_at"),
                            "created_by": row.get("project_created_by"),
                            "updated_at": row.get("project_updated_at"),
                            "updated_by": row.get("project_updated_by"),
                        }
                        if row.get("project_id") is not None and row.get("project_name") is not None
                        else None
                    ),
                    "created_at": row["document_created_at"],
                    "created_by": row["document_created_by"],
                    "updated_at": row["document_updated_at"],
                    "current_version": current_version,
                    "my_role": row["my_role"],
                }
            )
        return items

    async def _get_version_by_number(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        document_id: str,
        version_no: int,
    ) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT
                id,
                organization_id,
                document_id,
                version_no,
                content_hash,
                object_key,
                size_bytes,
                created_at,
                created_by,
                message,
                parent_version_id,
                compliance_result
            FROM document_versions
            WHERE organization_id = %s
              AND document_id = %s
              AND version_no = %s;
            """,
            (organization_id, document_id, version_no),
        )
        return await cur.fetchone()

    async def _get_version_by_id(
        self,
        cur: psycopg.AsyncCursor[Any],
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT
                id,
                organization_id,
                document_id,
                version_no,
                content_hash,
                object_key,
                size_bytes,
                created_at,
                created_by,
                message,
                parent_version_id,
                compliance_result
            FROM document_versions
            WHERE organization_id = %s
              AND document_id = %s
              AND id = %s;
            """,
            (organization_id, document_id, version_id),
        )
        return await cur.fetchone()

    async def _write_read_audit_if_requested(
        self,
        *,
        organization_id: str,
        actor_user_id: str | None,
        document_id: str,
        metadata: dict[str, Any],
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._write_audit(
                        cur,
                        organization_id=organization_id,
                        actor_user_id=actor_user_id,
                        action="document.read",
                        object_type="document",
                        object_id=document_id,
                        metadata=metadata,
                    )

    async def get_document_current(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                document_row, my_role = await self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles=DOCUMENT_READ_ROLES,
                )
                current_version_id = document_row["current_version_id"]
                if current_version_id is None:
                    raise DocumentHasNoVersionsError(f"Document {document_id} does not have versions yet.")

                version_row = await self._get_version_by_id(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    version_id=current_version_id,
                )
                if version_row is None:
                    raise DocumentVersionNotFoundError(
                        f"Current version id {current_version_id} for document {document_id} was not found."
                    )

        version = self._map_version_row(version_row)
        content_bytes = await self.minio_get(self.settings.minio_bucket, version["object_key"])

        await self._write_read_audit_if_requested(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            document_id=document_id,
            metadata={
                "document_id": document_id,
                "version_id": version["version_id"],
                "version_no": version["version_no"],
                "content_hash": version["content_hash"],
                "size_bytes": version["size_bytes"],
            },
        )

        return {
            "document": self._map_document_row(document_row, my_role=my_role),
            "version": version,
            "content_bytes": content_bytes,
        }

    async def get_document_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_no: int,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                document_row, my_role = await self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles=DOCUMENT_READ_ROLES,
                )
                version_row = await self._get_version_by_number(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    version_no=version_no,
                )
                if version_row is None:
                    raise DocumentVersionNotFoundError(
                        f"Version {version_no} for document {document_id} was not found."
                    )

        version = self._map_version_row(version_row)
        content_bytes = await self.minio_get(self.settings.minio_bucket, version["object_key"])

        await self._write_read_audit_if_requested(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            document_id=document_id,
            metadata={
                "document_id": document_id,
                "version_id": version["version_id"],
                "version_no": version["version_no"],
                "content_hash": version["content_hash"],
                "size_bytes": version["size_bytes"],
            },
        )

        return {
            "document": self._map_document_row(document_row, my_role=my_role),
            "version": version,
            "content_bytes": content_bytes,
        }

    async def list_versions(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles=DOCUMENT_READ_ROLES,
                )
                await cur.execute(
                    """
                    SELECT
                        id,
                        organization_id,
                        document_id,
                        version_no,
                        content_hash,
                        object_key,
                        size_bytes,
                        created_at,
                        created_by,
                        message,
                        parent_version_id,
                        compliance_result
                    FROM document_versions
                    WHERE organization_id = %s
                      AND document_id = %s
                    ORDER BY version_no DESC
                    LIMIT %s
                    OFFSET %s;
                    """,
                    (organization_id, document_id, limit, offset),
                )
                rows = await cur.fetchall()

        return [self._map_version_row(row) for row in rows]

    async def list_document_members(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str,
    ) -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles=DOCUMENT_READ_ROLES,
                )
                await cur.execute(
                    """
                    SELECT
                        dm.user_id,
                        dm.role,
                        dm.assigned_at,
                        dm.assigned_by,
                        u.primary_email,
                        u.first_name,
                        u.last_name
                    FROM document_memberships dm
                    LEFT JOIN users u
                           ON u.id = dm.user_id
                    WHERE dm.organization_id = %s
                      AND dm.document_id = %s
                    ORDER BY
                        CASE dm.role
                            WHEN 'owner' THEN 0
                            WHEN 'editor' THEN 1
                            ELSE 2
                        END,
                        dm.assigned_at ASC;
                    """,
                    (organization_id, document_id),
                )
                rows = await cur.fetchall()

        return [self._map_document_member_row(row) for row in rows]

    async def set_document_member_role(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str,
        target_user_id: str,
        role: str,
    ) -> dict[str, Any]:
        if role not in DOCUMENT_MEMBER_ROLES:
            raise ValueError(f"Unsupported document role: {role}")
        if not target_user_id.strip():
            raise ValueError("target_user_id is required.")

        target_user_id = target_user_id.strip()

        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._assert_document_access(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=actor_user_id,
                        allowed_roles={"owner"},
                    )
                    await self._ensure_user(cur, user_id=target_user_id)
                    await self._upsert_organization_membership(
                        cur,
                        organization_id=organization_id,
                        user_id=target_user_id,
                        role="member",
                        actor_user_id=actor_user_id,
                        overwrite_role=False,
                    )
                    await self._upsert_document_membership(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=target_user_id,
                        role=role,
                        actor_user_id=actor_user_id,
                    )
                    await self._write_audit(
                        cur,
                        organization_id=organization_id,
                        actor_user_id=actor_user_id,
                        action="document.member.upsert",
                        object_type="document",
                        object_id=document_id,
                        metadata={
                            "document_id": str(document_id),
                            "target_user_id": target_user_id,
                            "role": role,
                        },
                    )

                    await cur.execute(
                        """
                        SELECT
                            dm.user_id,
                            dm.role,
                            dm.assigned_at,
                            dm.assigned_by,
                            u.primary_email,
                            u.first_name,
                            u.last_name
                        FROM document_memberships dm
                        LEFT JOIN users u
                               ON u.id = dm.user_id
                        WHERE dm.organization_id = %s
                          AND dm.document_id = %s
                          AND dm.user_id = %s;
                        """,
                        (organization_id, document_id, target_user_id),
                    )
                    row = await cur.fetchone()

        if row is None:
            raise DocumentMemberNotFoundError(
                f"User {target_user_id} is not assigned to document {document_id}."
            )
        return self._map_document_member_row(row)

    async def remove_document_member(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str,
        target_user_id: str,
    ) -> None:
        if not target_user_id.strip():
            raise ValueError("target_user_id is required.")

        target_user_id = target_user_id.strip()

        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._assert_document_access(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=actor_user_id,
                        allowed_roles={"owner"},
                    )
                    existing_role = await self._get_document_membership_role(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=target_user_id,
                    )
                    if existing_role is None:
                        raise DocumentMemberNotFoundError(
                            f"User {target_user_id} is not assigned to document {document_id}."
                        )

                    if existing_role == "owner" and await self._count_document_owners(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                    ) <= 1:
                        raise DocumentOperationConflictError(
                            "Cannot remove the last owner from a document."
                        )

                    await cur.execute(
                        """
                        DELETE FROM document_memberships
                        WHERE organization_id = %s
                          AND document_id = %s
                          AND user_id = %s;
                        """,
                        (organization_id, document_id, target_user_id),
                    )
                    await self._write_audit(
                        cur,
                        organization_id=organization_id,
                        actor_user_id=actor_user_id,
                        action="document.member.delete",
                        object_type="document",
                        object_id=document_id,
                        metadata={
                            "document_id": str(document_id),
                            "target_user_id": target_user_id,
                        },
                    )

    async def gc_unreferenced_objects(
        self,
        *,
        dry_run: bool = True,
        max_delete: int = 1000,
    ) -> dict[str, Any]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT DISTINCT object_key FROM document_versions;")
                referenced_keys = {row["object_key"] for row in await cur.fetchall()}

        bucket = self.settings.minio_bucket

        def _scan() -> tuple[int, list[str], int]:
            scanned = 0
            unreferenced: list[str] = []
            referenced = 0
            for obj in self.minio_client.list_objects(bucket_name=bucket, prefix="objects/", recursive=True):
                scanned += 1
                if obj.object_name in referenced_keys:
                    referenced += 1
                else:
                    unreferenced.append(obj.object_name)
            return scanned, unreferenced, referenced

        scanned, unreferenced, referenced = await asyncio.to_thread(_scan)

        deleted: list[str] = []
        if not dry_run:
            def _delete(keys: list[str]) -> list[str]:
                done: list[str] = []
                for key in keys:
                    self.minio_client.remove_object(bucket, key)
                    done.append(key)
                return done

            deleted = await asyncio.to_thread(_delete, unreferenced[:max_delete])

        return {
            "dry_run": dry_run,
            "scanned_objects": scanned,
            "referenced_objects": referenced,
            "unreferenced_objects": len(unreferenced),
            "candidate_keys": unreferenced[:max_delete],
            "deleted_keys": deleted,
        }

    # ------------------------------------------------------------------
    # Compliance result persistence
    # ------------------------------------------------------------------

    async def save_compliance_result(
        self,
        *,
        organization_id: str,
        version_id: str,
        result: dict[str, Any],
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE document_versions
                           SET compliance_result = %s
                         WHERE id = %s
                           AND organization_id = %s;
                        """,
                        (Json(result), version_id, organization_id),
                    )
                    if cur.rowcount == 0:
                        raise DocumentNotFoundError(f"Version {version_id} not found")

    async def save_project_compliance_result(
        self,
        *,
        organization_id: str,
        project_id: str,
        result: dict[str, Any],
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE folders
                           SET compliance_result = %s,
                               updated_at = NOW()
                         WHERE id = %s
                           AND organization_id = %s;
                        """,
                        (Json(result), project_id, organization_id),
                    )
                    if cur.rowcount == 0:
                        raise ProjectNotFoundError(f"Project {project_id} not found")

    async def get_project_compliance_result(
        self,
        *,
        organization_id: str,
        project_id: str,
        actor_user_id: str,
    ) -> dict[str, Any] | None:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await self._ensure_user_in_organization(
                    cur, organization_id=organization_id, user_id=actor_user_id
                )
                row = await self._assert_project_in_org(
                    cur,
                    organization_id=organization_id,
                    project_id=project_id,
                )
        return row.get("compliance_result")

    async def get_project_evaluation_context(
        self,
        *,
        organization_id: str,
        project_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                organization_role = await self._ensure_user_in_organization(
                    cur,
                    organization_id=organization_id,
                    user_id=actor_user_id,
                )
                project_row = await self._assert_project_in_org(
                    cur,
                    organization_id=organization_id,
                    project_id=project_id,
                )

                await cur.execute(
                    """
                    SELECT
                        d.id AS document_id,
                        d.title,
                        d.current_version_id AS version_id,
                        v.version_no,
                        dc.id AS chunk_id,
                        dc.chunk_level,
                        dc.title AS chunk_title,
                        dc.start_page,
                        dc.end_page,
                        dc.text_content,
                        CASE
                            WHEN %s = 'owner' THEN 'owner'
                            ELSE dm.role
                        END AS my_role
                    FROM documents d
                    LEFT JOIN document_memberships dm
                           ON dm.organization_id = d.organization_id
                          AND dm.document_id = d.id
                          AND dm.user_id = %s
                    LEFT JOIN document_versions v
                           ON v.id = d.current_version_id
                          AND v.organization_id = d.organization_id
                    LEFT JOIN document_chunks dc
                           ON dc.organization_id = d.organization_id
                          AND dc.document_id = d.id
                          AND dc.version_id = d.current_version_id
                    WHERE d.organization_id = %s
                      AND d.folder_id = %s
                      AND (%s = 'owner' OR dm.user_id IS NOT NULL)
                    ORDER BY d.updated_at DESC, dc.start_page ASC, dc.created_at ASC;
                    """,
                    (
                        organization_role,
                        actor_user_id,
                        organization_id,
                        project_id,
                        organization_role,
                    ),
                )
                rows = await cur.fetchall()

        documents_by_id: dict[str, dict[str, Any]] = {}
        chunks: list[dict[str, Any]] = []

        for row in rows:
            document_id = str(row["document_id"])
            if document_id not in documents_by_id:
                documents_by_id[document_id] = {
                    "document_id": document_id,
                    "title": row.get("title"),
                    "version_id": row.get("version_id"),
                    "version_no": row.get("version_no"),
                }

            if row.get("chunk_id") is None:
                continue

            document_title = row.get("title") or f"Document {document_id}"
            chunk_title = row.get("chunk_title") or "Unnamed Section"
            chunks.append(
                {
                    "id": str(row["chunk_id"]),
                    "organization_id": organization_id,
                    "document_id": document_id,
                    "version_id": row.get("version_id"),
                    "chunk_level": row.get("chunk_level", 1),
                    "title": f"{document_title} :: {chunk_title}",
                    "start_page": row.get("start_page", 1),
                    "end_page": row.get("end_page", 1),
                    "text_content": row.get("text_content", ""),
                }
            )

        return {
            "project": self._map_project_row(project_row),
            "documents": list(documents_by_id.values()),
            "chunks": chunks,
        }

    # ------------------------------------------------------------------
    # Project management
    # ------------------------------------------------------------------

    async def create_project(
        self,
        *,
        organization_id: str,
        name: str,
        description: str | None,
        legislation_template_ids: list[str] | None,
        actor_user_id: str,
    ) -> dict[str, Any]:
        project_name = name.strip()
        if not project_name:
            raise ValueError("Project name is required.")

        project_description = description.strip() if description is not None else None
        if project_description == "":
            project_description = None

        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._ensure_user_in_organization(
                        cur, organization_id=organization_id, user_id=actor_user_id
                    )
                    await cur.execute(
                        """
                        INSERT INTO folders (
                            organization_id,
                            name,
                            description,
                            legislation_template_ids,
                            created_by,
                            updated_by
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING
                            id,
                            organization_id,
                            name,
                            description,
                            legislation_template_ids,
                            created_at,
                            created_by,
                            updated_at,
                            updated_by,
                            0 AS document_count;
                        """,
                        (
                            organization_id,
                            project_name,
                            project_description,
                            Json(legislation_template_ids or []),
                            actor_user_id,
                            actor_user_id,
                        ),
                    )
                    row = await cur.fetchone()

        if row is None:
            raise DocumentStorageError("Project creation failed.")
        return self._map_project_row(row)

    async def list_projects(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
    ) -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await self._ensure_user_in_organization(
                    cur, organization_id=organization_id, user_id=actor_user_id
                )
                await cur.execute(
                    """
                    SELECT
                        f.id,
                        f.organization_id,
                        f.name,
                        f.description AS description,
                        f.legislation_template_ids,
                        f.created_at,
                        f.created_by,
                        f.updated_at,
                        f.updated_by,
                        COUNT(d.id)::INT AS document_count
                    FROM folders f
                    LEFT JOIN documents d
                           ON d.folder_id = f.id
                          AND d.organization_id = f.organization_id
                    WHERE f.organization_id = %s
                    GROUP BY
                        f.id,
                        f.organization_id,
                        f.name,
                        f.description,
                        f.legislation_template_ids,
                        f.created_at,
                        f.created_by,
                        f.updated_at,
                        f.updated_by
                    ORDER BY f.updated_at DESC, f.name ASC;
                    """,
                    (organization_id,),
                )
                rows = await cur.fetchall()

        return [self._map_project_row(row) for row in rows]

    async def get_project(
        self,
        *,
        organization_id: str,
        project_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await self._ensure_user_in_organization(
                    cur, organization_id=organization_id, user_id=actor_user_id
                )
                row = await self._assert_project_in_org(
                    cur,
                    organization_id=organization_id,
                    project_id=project_id,
                )

        return self._map_project_row(row)

    async def update_project(
        self,
        *,
        organization_id: str,
        project_id: str,
        actor_user_id: str,
        name: str | None = None,
        description: str | None = None,
        legislation_template_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if name is None and description is None and legislation_template_ids is None:
            raise ValueError("At least one project field must be provided.")

        project_name = name.strip() if name is not None else None
        if project_name == "":
            raise ValueError("Project name cannot be empty.")

        project_description = description.strip() if description is not None else None
        if description is not None and project_description == "":
            project_description = None

        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._ensure_user_in_organization(
                        cur, organization_id=organization_id, user_id=actor_user_id
                    )
                    await self._assert_project_in_org(
                        cur,
                        organization_id=organization_id,
                        project_id=project_id,
                        for_update=True,
                    )
                    await cur.execute(
                        """
                        UPDATE folders
                        SET name = COALESCE(%s, name),
                            description = CASE
                                WHEN %s THEN %s
                                ELSE description
                            END,
                            legislation_template_ids = CASE
                                WHEN %s THEN %s
                                ELSE legislation_template_ids
                            END,
                            updated_at = NOW(),
                            updated_by = %s
                        WHERE id = %s
                          AND organization_id = %s
                        RETURNING
                            id,
                            organization_id,
                            name,
                            description,
                            legislation_template_ids,
                            created_at,
                            created_by,
                            updated_at,
                            updated_by,
                            (
                                SELECT COUNT(*)::INT
                                FROM documents d
                                WHERE d.folder_id = folders.id
                                  AND d.organization_id = folders.organization_id
                            ) AS document_count;
                        """,
                        (
                            project_name,
                            description is not None,
                            project_description,
                            legislation_template_ids is not None,
                            Json(legislation_template_ids or []),
                            actor_user_id,
                            project_id,
                            organization_id,
                        ),
                    )
                    row = await cur.fetchone()

        if row is None:
            raise ProjectNotFoundError(f"Project {project_id} was not found.")
        return self._map_project_row(row)

    async def delete_project(
        self,
        *,
        organization_id: str,
        project_id: str,
        actor_user_id: str,
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._ensure_user_in_organization(
                        cur, organization_id=organization_id, user_id=actor_user_id
                    )
                    project_row = await self._assert_project_in_org(
                        cur,
                        organization_id=organization_id,
                        project_id=project_id,
                        for_update=True,
                    )
                    if int(project_row.get("document_count") or 0) > 0:
                        raise DocumentOperationConflictError(
                            f"Project {project_id} cannot be deleted while documents are still assigned to it."
                        )
                    await cur.execute(
                        """
                        DELETE FROM folders
                        WHERE id = %s
                          AND organization_id = %s;
                        """,
                        (project_id, organization_id),
                    )
                    if cur.rowcount == 0:
                        raise ProjectNotFoundError(f"Project {project_id} was not found.")

    async def move_document_to_project(
        self,
        *,
        document_id: str,
        organization_id: str,
        project_id: str,
        actor_user_id: str,
    ) -> None:
        if not project_id.strip():
            raise ValueError("project_id is required.")

        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._assert_document_access(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=actor_user_id,
                        allowed_roles=DOCUMENT_WRITE_ROLES,
                    )
                    await self._assert_project_in_org(
                        cur,
                        organization_id=organization_id,
                        project_id=project_id,
                    )
                    await cur.execute(
                        """
                        UPDATE documents
                        SET folder_id = %s,
                            updated_at = NOW()
                        WHERE id = %s
                          AND organization_id = %s;
                        """,
                        (project_id, document_id, organization_id),
                    )
                    if cur.rowcount == 0:
                        raise DocumentNotFoundError(f"Document {document_id} not found")

    # ------------------------------------------------------------------
    # Folder management
    # ------------------------------------------------------------------

    async def create_folder(
        self,
        *,
        organization_id: str,
        name: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._ensure_user_in_organization(
                        cur, organization_id=organization_id, user_id=actor_user_id
                    )
                    await cur.execute(
                        """
                        INSERT INTO folders (organization_id, name, created_by)
                        VALUES (%s, %s, %s)
                        RETURNING id, organization_id, name, created_at, created_by;
                        """,
                        (organization_id, name, actor_user_id),
                    )
                    return await cur.fetchone()  # type: ignore[return-value]

    async def list_folders(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
    ) -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.cursor() as cur:
                await self._ensure_user_in_organization(
                    cur, organization_id=organization_id, user_id=actor_user_id
                )
                await cur.execute(
                    """
                    SELECT id, organization_id, name, created_at, created_by
                    FROM folders
                    WHERE organization_id = %s
                    ORDER BY name ASC;
                    """,
                    (organization_id,),
                )
                return await cur.fetchall()

    async def rename_folder(
        self,
        *,
        folder_id: str,
        organization_id: str,
        name: str,
        actor_user_id: str,
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._ensure_user_in_organization(
                        cur, organization_id=organization_id, user_id=actor_user_id
                    )
                    await cur.execute(
                        """
                        UPDATE folders SET name = %s
                        WHERE id = %s AND organization_id = %s;
                        """,
                        (name, folder_id, organization_id),
                    )
                    if cur.rowcount == 0:
                        raise DocumentNotFoundError(f"Folder {folder_id} not found")

    async def delete_folder(
        self,
        *,
        folder_id: str,
        organization_id: str,
        actor_user_id: str,
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._ensure_user_in_organization(
                        cur, organization_id=organization_id, user_id=actor_user_id
                    )
                    # documents.folder_id becomes NULL via ON DELETE SET NULL
                    await cur.execute(
                        "DELETE FROM folders WHERE id = %s AND organization_id = %s;",
                        (folder_id, organization_id),
                    )
                    if cur.rowcount == 0:
                        raise DocumentNotFoundError(f"Folder {folder_id} not found")

    async def move_document_to_folder(
        self,
        *,
        document_id: str,
        organization_id: str,
        folder_id: str | None,
        actor_user_id: str,
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await self._assert_document_access(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=actor_user_id,
                        allowed_roles=DOCUMENT_WRITE_ROLES,
                    )
                    await cur.execute(
                        """
                        UPDATE documents SET folder_id = %s
                        WHERE id = %s AND organization_id = %s;
                        """,
                        (folder_id, document_id, organization_id),
                    )
                    if cur.rowcount == 0:
                        raise DocumentNotFoundError(f"Document {document_id} not found")
