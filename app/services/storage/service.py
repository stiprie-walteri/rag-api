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

    def minio_put_if_missing(self, bucket: str, key: str, payload: bytes) -> bool:
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

    def minio_get(self, bucket: str, key: str) -> bytes:
        response = self.minio_client.get_object(bucket_name=bucket, object_name=key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def _ensure_user(
        self,
        cur: psycopg.Cursor[Any],
        *,
        user_id: str,
        primary_email: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        cur.execute(
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

    def _get_organization_by_id(
        self,
        cur: psycopg.Cursor[Any],
        organization_id: str,
    ) -> dict[str, Any] | None:
        cur.execute(
            """
            SELECT id, clerk_org_id, clerk_org_slug, name, created_at
            FROM organizations
            WHERE id = %s;
            """,
            (organization_id,),
        )
        return cur.fetchone()

    def _get_organization_by_clerk_org_id(
        self,
        cur: psycopg.Cursor[Any],
        clerk_org_id: str | None = None,
    ) -> dict[str, Any] | None:
        cur.execute(
            """
            SELECT id, clerk_org_id, clerk_org_slug, name, created_at
            FROM organizations
            WHERE clerk_org_id = %s;
            """,
            (clerk_org_id,),
        )
        return cur.fetchone()

    def _upsert_organization_membership(
        self,
        cur: psycopg.Cursor[Any],
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
            cur.execute(
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

        cur.execute(
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

    def _get_organization_membership_role(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        user_id: str,
    ) -> str | None:
        cur.execute(
            """
            SELECT role
            FROM organization_memberships
            WHERE organization_id = %s
              AND user_id = %s;
            """,
            (organization_id, user_id),
        )
        row = cur.fetchone()
        return row["role"] if row else None

    def _ensure_user_in_organization(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        user_id: str,
    ) -> str:
        REQUIRE_ORG_VALIDATION = os.getenv("REQUIRE_ORG_VALIDATION", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not REQUIRE_ORG_VALIDATION:
            return "owner"

        role = self._get_organization_membership_role(cur, organization_id=organization_id, user_id=user_id)
        if role is None:
            raise DocumentAccessDeniedError(
                f"User {user_id} is not a member of organization {organization_id}."
            )
        return role

    def _upsert_document_membership(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        document_id: str,
        user_id: str,
        role: str,
        actor_user_id: str,
    ) -> None:
        if role not in DOCUMENT_MEMBER_ROLES:
            raise ValueError(f"Unsupported document role: {role}")

        cur.execute(
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

    def _get_document_membership_role(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        document_id: str,
        user_id: str,
    ) -> str | None:
        cur.execute(
            """
            SELECT role
            FROM document_memberships
            WHERE organization_id = %s
              AND document_id = %s
              AND user_id = %s;
            """,
            (organization_id, document_id, user_id),
        )
        row = cur.fetchone()
        return row["role"] if row else None

    def _count_document_owners(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        document_id: str,
    ) -> int:
        cur.execute(
            """
            SELECT COUNT(*) AS owner_count
            FROM document_memberships
            WHERE organization_id = %s
              AND document_id = %s
              AND role = 'owner';
            """,
            (organization_id, document_id),
        )
        row = cur.fetchone()
        return int(row["owner_count"]) if row else 0

    def _write_audit(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        actor_user_id: str,
        action: str,
        object_type: str,
        object_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        cur.execute(
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

    def _get_document_for_org(
        self,
        cur: psycopg.Cursor[Any],
        organization_id: str,
        document_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        lock_clause = " FOR UPDATE" if for_update else ""
        cur.execute(
            f"""
            SELECT
                id,
                organization_id,
                title,
                current_version_id,
                created_at,
                created_by,
                updated_at
            FROM documents
            WHERE id = %s
              AND organization_id = %s{lock_clause};
            """,
            (document_id, organization_id),
        )
        return cur.fetchone()

    def _assert_document_in_org(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        document_id: str,
        for_update: bool = False,
    ) -> dict[str, Any]:
        row = self._get_document_for_org(
            cur,
            organization_id=organization_id,
            document_id=document_id,
            for_update=for_update,
        )
        if row is not None:
            return row

        cur.execute("SELECT organization_id FROM documents WHERE id = %s;", (document_id,))
        existing = cur.fetchone()
        if existing is None:
            raise DocumentNotFoundError(f"Document {document_id} was not found.")
        raise OrganizationMismatchError(
            f"Document {document_id} belongs to organization {existing['organization_id']}, not {organization_id}."
        )

    def _assert_document_access(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        document_id: str,
        user_id: str,
        allowed_roles: set[str],
        for_update: bool = False,
    ) -> tuple[dict[str, Any], str]:
        organization_role = self._ensure_user_in_organization(
            cur,
            organization_id=organization_id,
            user_id=user_id,
        )
        
        REQUIRE_ORG_VALIDATION = os.getenv("REQUIRE_ORG_VALIDATION", "true").strip().lower() in {"1", "true", "yes", "on"}
        if not REQUIRE_ORG_VALIDATION:
            # When validation is off, return a mock document row and 'owner' role.
            # Downstream logic that uses this document row usually gets document properties,
            # but since validation is off, let's gracefully fall back to a dummy row or fetch the row without asserting org match.
            cur.execute("SELECT * FROM documents WHERE id = %s;", (document_id,))
            doc_row = cur.fetchone()
            if doc_row is None:
                raise DocumentNotFoundError(f"Document {document_id} was not found.")
            return doc_row, "owner"

        document_row = self._assert_document_in_org(
            cur,
            organization_id=organization_id,
            document_id=document_id,
            for_update=for_update,
        )

        if organization_role == "owner":
            return document_row, "owner"

        document_role = self._get_document_membership_role(
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
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "updated_at": row["updated_at"],
            "current_version_id": row["current_version_id"],
            "my_role": my_role,
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

    def sync_authenticated_user(
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

        private_org_key = f"user:{clerk_user_id}"
        local_org_role = "owner"
        organization_name = primary_email or "Personal workspace"

        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._ensure_user(
                        cur,
                        user_id=clerk_user_id,
                        primary_email=primary_email,
                        first_name=first_name,
                        last_name=last_name,
                    )

                    organization_row = None
                    existing_private_org_row = self._get_organization_by_clerk_org_id(cur, private_org_key)
                    
                    REQUIRE_ORG_VALIDATION = os.getenv("REQUIRE_ORG_VALIDATION", "true").strip().lower() in {"1", "true", "yes", "on"}

                    if not REQUIRE_ORG_VALIDATION and requested_organization_id is not None:
                        # Auto-upsert the requested organization to satisfy postgres foreign keys
                        cur.execute(
                            """
                            INSERT INTO organizations (
                                id, clerk_org_id, clerk_org_slug, name, created_at
                            ) VALUES (%s, NULL, %s, %s, NOW())
                            ON CONFLICT (id) DO NOTHING;
                            """,
                            (requested_organization_id, None, organization_name)
                        )
                        organization_row = self._get_organization_by_id(cur, requested_organization_id)
                    elif requested_organization_id is not None:
                        organization_row = self._get_organization_by_id(cur, requested_organization_id)
                        if organization_row is None:
                            raise OrganizationNotFoundError(
                                f"Organization {requested_organization_id} was not found."
                            )

                        if (
                            existing_private_org_row is not None
                            and organization_row["id"] != existing_private_org_row["id"]
                        ):
                            raise OrganizationMismatchError(
                                f"Organization {requested_organization_id} does not match the authenticated "
                                f"user's private workspace {existing_private_org_row['id']}."
                            )

                        existing_clerk_org_id = organization_row["clerk_org_id"]
                        if existing_clerk_org_id is None:
                            membership_role = self._get_organization_membership_role(
                                cur,
                                organization_id=requested_organization_id,
                                user_id=clerk_user_id,
                            )
                            if membership_role != "owner":
                                raise DocumentAccessDeniedError(
                                    f"User {clerk_user_id} cannot claim organization {requested_organization_id}."
                                )
                            cur.execute(
                                """
                                UPDATE organizations
                                SET clerk_org_id = %s,
                                    clerk_org_slug = NULL,
                                    name = COALESCE(%s, name)
                                WHERE id = %s;
                                """,
                                (private_org_key, organization_name, requested_organization_id),
                            )
                        elif existing_clerk_org_id != private_org_key:
                            raise OrganizationMismatchError(
                                f"Organization {requested_organization_id} is linked to {existing_clerk_org_id}, not {private_org_key}."
                            )
                        organization_row = self._get_organization_by_id(cur, requested_organization_id)

                    if organization_row is None:
                        organization_row = existing_private_org_row

                    if organization_row is None:
                        if not create_if_missing:
                            raise OrganizationNotFoundError(
                                "No private workspace exists for the authenticated user."
                            )
                        organization_id = str(uuid.uuid4())
                        cur.execute(
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
                            (organization_id, private_org_key, None, organization_name),
                        )
                        organization_row = self._get_organization_by_id(cur, organization_id)
                    else:
                        cur.execute(
                            """
                            UPDATE organizations
                            SET clerk_org_slug = NULL,
                                name = COALESCE(%s, name)
                            WHERE id = %s;
                            """,
                            (organization_name, organization_row["id"]),
                        )
                        organization_row = self._get_organization_by_id(cur, organization_row["id"])

                    self._upsert_organization_membership(
                        cur,
                        organization_id=organization_row["id"],
                        user_id=clerk_user_id,
                        role=local_org_role,
                        actor_user_id=clerk_user_id,
                        overwrite_role=True,
                    )

        return {
            "organization_id": organization_row["id"],
            "clerk_org_id": private_org_key,
            "clerk_org_slug": None,
            "organization_role": local_org_role,
            "user_id": clerk_user_id,
        }

    def create_version(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
        markdown_bytes: bytes,
        document_id: str | None = None,
        title: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        if not markdown_bytes:
            raise ValueError("Document content cannot be empty.")

        content_hash = self.compute_hash(markdown_bytes)
        object_key = f"objects/{content_hash}.md"
        size_bytes = len(markdown_bytes)
        uploaded_object = self.minio_put_if_missing(self.settings.minio_bucket, object_key, markdown_bytes)

        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._ensure_user(cur, user_id=actor_user_id)
                    self._ensure_user_in_organization(
                        cur,
                        organization_id=organization_id,
                        user_id=actor_user_id,
                    )

                    if document_id is None:
                        document_id = str(uuid.uuid4())
                        parent_version_id = None
                        version_no = 1
                        cur.execute(
                            """
                            INSERT INTO documents (
                                id,
                                organization_id,
                                title,
                                current_version_id,
                                created_at,
                                created_by,
                                updated_at
                            )
                            VALUES (%s, %s, %s, NULL, NOW(), %s, NOW());
                            """,
                            (document_id, organization_id, title, actor_user_id),
                        )
                        self._upsert_document_membership(
                            cur,
                            organization_id=organization_id,
                            document_id=document_id,
                            user_id=actor_user_id,
                            role="owner",
                            actor_user_id=actor_user_id,
                        )
                    else:
                        document_row, _ = self._assert_document_access(
                            cur,
                            organization_id=organization_id,
                            document_id=document_id,
                            user_id=actor_user_id,
                            allowed_roles=DOCUMENT_WRITE_ROLES,
                            for_update=True,
                        )
                        parent_version_id = document_row["current_version_id"]

                        if parent_version_id is None:
                            version_no = 1
                        else:
                            cur.execute(
                                """
                                SELECT version_no
                                FROM document_versions
                                WHERE id = %s
                                  AND organization_id = %s
                                  AND document_id = %s;
                                """,
                                (parent_version_id, organization_id, document_id),
                            )
                            parent_version_row = cur.fetchone()
                            if parent_version_row is None:
                                raise DocumentVersionNotFoundError(
                                    f"Current version for document {document_id} was not found."
                                )
                            version_no = parent_version_row["version_no"] + 1

                        if title is not None:
                            cur.execute(
                                """
                                UPDATE documents
                                SET title = %s
                                WHERE id = %s
                                  AND organization_id = %s;
                                """,
                                (title, document_id, organization_id),
                            )

                    version_id = str(uuid.uuid4())
                    cur.execute(
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

                    cur.execute(
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
                        "version_id": version_id,
                        "version_no": version_no,
                        "content_hash": content_hash,
                        "size_bytes": size_bytes,
                        "object_key": object_key,
                        "object_uploaded": uploaded_object,
                    }

                    self._write_audit(
                        cur,
                        organization_id=organization_id,
                        actor_user_id=actor_user_id,
                        action="document.upload",
                        object_type="document",
                        object_id=document_id,
                        metadata=audit_metadata,
                    )
                    self._write_audit(
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
            "version_id": version_id,
            "version_no": version_no,
            "content_hash": content_hash,
            "object_key": object_key,
            "size_bytes": size_bytes,
        }

    def save_document_chunks(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        chunks: list[dict],
    ) -> None:
        if not chunks:
            return

        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    for chunk in chunks:
                        chunk_id = str(uuid.uuid4())
                        cur.execute(
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

    def get_document_chunks(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        actor_user_id: str,
        title: str | None = None,
        chunk_level: int | None = None,
    ) -> list[dict]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                self._assert_document_access(
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

                cur.execute(query, tuple(params))
                rows = cur.fetchall()

        return [
            {**row, "id": str(row["id"])}
            for row in rows
        ]

    def delete_document(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str,
    ) -> dict:
        """Delete a document and all its associated chunks, versions, and MinIO objects.

        Returns a summary of what was deleted.
        """
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                # Assert the user has owner-level access to the document
                self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    actor_user_id=actor_user_id,
                    required_roles={"owner"},
                )

                # Collect all MinIO object keys from all versions to delete later
                cur.execute(
                    """
                    SELECT object_key FROM document_versions
                    WHERE document_id = %s AND organization_id = %s;
                    """,
                    (document_id, organization_id),
                )
                version_rows = cur.fetchall()
                object_keys = [r["object_key"] for r in version_rows if r.get("object_key")]

                # Count chunks for reporting
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM document_chunks WHERE document_id = %s AND organization_id = %s;",
                    (document_id, organization_id),
                )
                chunk_count = (cur.fetchone() or {}).get("cnt", 0)

                # Delete chunks first (FK child of versions)
                cur.execute(
                    "DELETE FROM document_chunks WHERE document_id = %s AND organization_id = %s;",
                    (document_id, organization_id),
                )

                # Delete versions (FK child of documents)
                cur.execute(
                    "DELETE FROM document_versions WHERE document_id = %s AND organization_id = %s;",
                    (document_id, organization_id),
                )

                # Delete the document record itself
                cur.execute(
                    "DELETE FROM documents WHERE id = %s AND organization_id = %s;",
                    (document_id, organization_id),
                )

                conn.commit()

        # Delete MinIO objects outside the DB transaction (best-effort)
        deleted_objects: list[str] = []
        failed_objects: list[str] = []
        for key in object_keys:
            try:
                self.minio_client.remove_object(self.settings.minio_bucket, key)
                deleted_objects.append(key)
            except S3Error as exc:
                logger.warning("Failed to delete MinIO object %s: %s", key, exc)
                failed_objects.append(key)

        return {
            "document_id": document_id,
            "organization_id": organization_id,
            "chunks_deleted": chunk_count,
            "versions_deleted": len(version_rows),
            "objects_deleted": len(deleted_objects),
            "objects_failed": len(failed_objects),
        }

    def list_documents(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                organization_role = self._ensure_user_in_organization(
                    cur,
                    organization_id=organization_id,
                    user_id=actor_user_id,
                )
                cur.execute(
                    """
                    SELECT
                        d.id AS document_id,
                        d.organization_id,
                        d.title,
                        d.created_at AS document_created_at,
                        d.created_by AS document_created_by,
                        d.updated_at AS document_updated_at,
                        d.current_version_id,
                        v.id AS version_id,
                        v.version_no,
                        v.content_hash,
                        v.object_key,
                        v.size_bytes,
                        v.created_at AS version_created_at,
                        v.created_by AS version_created_by,
                        v.message,
                        v.parent_version_id,
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
                    WHERE d.organization_id = %s
                      AND (%s = 'owner' OR dm.user_id IS NOT NULL)
                    ORDER BY d.updated_at DESC
                    LIMIT %s
                    OFFSET %s;
                    """,
                    (organization_role, actor_user_id, organization_id, organization_role, limit, offset),
                )
                rows = cur.fetchall()

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
                }

            items.append(
                {
                    "document_id": row["document_id"],
                    "organization_id": row["organization_id"],
                    "title": row["title"],
                    "created_at": row["document_created_at"],
                    "created_by": row["document_created_by"],
                    "updated_at": row["document_updated_at"],
                    "current_version": current_version,
                    "my_role": row["my_role"],
                }
            )
        return items

    def _get_version_by_number(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        document_id: str,
        version_no: int,
    ) -> dict[str, Any] | None:
        cur.execute(
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
                parent_version_id
            FROM document_versions
            WHERE organization_id = %s
              AND document_id = %s
              AND version_no = %s;
            """,
            (organization_id, document_id, version_no),
        )
        return cur.fetchone()

    def _get_version_by_id(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> dict[str, Any] | None:
        cur.execute(
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
                parent_version_id
            FROM document_versions
            WHERE organization_id = %s
              AND document_id = %s
              AND id = %s;
            """,
            (organization_id, document_id, version_id),
        )
        return cur.fetchone()

    def _write_read_audit_if_requested(
        self,
        *,
        organization_id: str,
        actor_user_id: str | None,
        document_id: str,
        metadata: dict[str, Any],
    ) -> None:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._write_audit(
                        cur,
                        organization_id=organization_id,
                        actor_user_id=actor_user_id,
                        action="document.read",
                        object_type="document",
                        object_id=document_id,
                        metadata=metadata,
                    )

    def get_document_current(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                document_row, my_role = self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles=DOCUMENT_READ_ROLES,
                )
                current_version_id = document_row["current_version_id"]
                if current_version_id is None:
                    raise DocumentHasNoVersionsError(f"Document {document_id} does not have versions yet.")

                version_row = self._get_version_by_id(
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
        content_bytes = self.minio_get(self.settings.minio_bucket, version["object_key"])

        self._write_read_audit_if_requested(
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

    def get_document_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_no: int,
        actor_user_id: str,
    ) -> dict[str, Any]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                document_row, my_role = self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles=DOCUMENT_READ_ROLES,
                )
                version_row = self._get_version_by_number(
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
        content_bytes = self.minio_get(self.settings.minio_bucket, version["object_key"])

        self._write_read_audit_if_requested(
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

    def list_versions(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles=DOCUMENT_READ_ROLES,
                )
                cur.execute(
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
                        parent_version_id
                    FROM document_versions
                    WHERE organization_id = %s
                      AND document_id = %s
                    ORDER BY version_no DESC
                    LIMIT %s
                    OFFSET %s;
                    """,
                    (organization_id, document_id, limit, offset),
                )
                rows = cur.fetchall()

        return [self._map_version_row(row) for row in rows]

    def list_document_members(
        self,
        *,
        organization_id: str,
        document_id: str,
        actor_user_id: str,
    ) -> list[dict[str, Any]]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                self._assert_document_access(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
                    user_id=actor_user_id,
                    allowed_roles=DOCUMENT_READ_ROLES,
                )
                cur.execute(
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
                rows = cur.fetchall()

        return [self._map_document_member_row(row) for row in rows]

    def set_document_member_role(
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

        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._assert_document_access(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=actor_user_id,
                        allowed_roles={"owner"},
                    )
                    self._ensure_user(cur, user_id=target_user_id)
                    self._upsert_organization_membership(
                        cur,
                        organization_id=organization_id,
                        user_id=target_user_id,
                        role="member",
                        actor_user_id=actor_user_id,
                        overwrite_role=False,
                    )
                    self._upsert_document_membership(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=target_user_id,
                        role=role,
                        actor_user_id=actor_user_id,
                    )
                    self._write_audit(
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

                    cur.execute(
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
                    row = cur.fetchone()

        if row is None:
            raise DocumentMemberNotFoundError(
                f"User {target_user_id} is not assigned to document {document_id}."
            )
        return self._map_document_member_row(row)

    def remove_document_member(
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

        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._assert_document_access(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=actor_user_id,
                        allowed_roles={"owner"},
                    )
                    existing_role = self._get_document_membership_role(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                        user_id=target_user_id,
                    )
                    if existing_role is None:
                        raise DocumentMemberNotFoundError(
                            f"User {target_user_id} is not assigned to document {document_id}."
                        )

                    if existing_role == "owner" and self._count_document_owners(
                        cur,
                        organization_id=organization_id,
                        document_id=document_id,
                    ) <= 1:
                        raise DocumentOperationConflictError(
                            "Cannot remove the last owner from a document."
                        )

                    cur.execute(
                        """
                        DELETE FROM document_memberships
                        WHERE organization_id = %s
                          AND document_id = %s
                          AND user_id = %s;
                        """,
                        (organization_id, document_id, target_user_id),
                    )
                    self._write_audit(
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

    def gc_unreferenced_objects(
        self,
        *,
        dry_run: bool = True,
        max_delete: int = 1000,
    ) -> dict[str, Any]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT object_key FROM document_versions;")
                referenced_keys = {row["object_key"] for row in cur.fetchall()}

        scanned = 0
        unreferenced: list[str] = []
        referenced = 0
        for obj in self.minio_client.list_objects(
            bucket_name=self.settings.minio_bucket,
            prefix="objects/",
            recursive=True,
        ):
            scanned += 1
            object_key = obj.object_name
            if object_key in referenced_keys:
                referenced += 1
                continue
            unreferenced.append(object_key)

        deleted: list[str] = []
        if not dry_run:
            for object_key in unreferenced[:max_delete]:
                self.minio_client.remove_object(self.settings.minio_bucket, object_key)
                deleted.append(object_key)

        return {
            "dry_run": dry_run,
            "scanned_objects": scanned,
            "referenced_objects": referenced,
            "unreferenced_objects": len(unreferenced),
            "candidate_keys": unreferenced[:max_delete],
            "deleted_keys": deleted,
        }
