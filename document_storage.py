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


class DocumentStorageError(Exception):
    pass


class DocumentNotFoundError(DocumentStorageError):
    pass


class OrganizationMismatchError(DocumentStorageError):
    pass


class DocumentVersionNotFoundError(DocumentStorageError):
    pass


class DocumentHasNoVersionsError(DocumentStorageError):
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

    def _ensure_organization_exists(self, cur: psycopg.Cursor[Any], organization_id: uuid.UUID) -> None:
        cur.execute(
            """
            INSERT INTO organizations (id)
            VALUES (%s)
            ON CONFLICT (id) DO NOTHING;
            """,
            (organization_id,),
        )

    def _write_audit(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: uuid.UUID,
        actor_user_id: str,
        action: str,
        object_type: str,
        object_id: uuid.UUID,
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
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
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
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
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

    def _map_document_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_id": row["id"],
            "organization_id": row["organization_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "updated_at": row["updated_at"],
            "current_version_id": row["current_version_id"],
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

    def create_version(
        self,
        *,
        organization_id: uuid.UUID,
        actor_user_id: str,
        markdown_bytes: bytes,
        document_id: uuid.UUID | None = None,
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
                    self._ensure_organization_exists(cur, organization_id)

                    if document_id is None:
                        document_id = uuid.uuid4()
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
                    else:
                        document_row = self._assert_document_in_org(
                            cur,
                            organization_id=organization_id,
                            document_id=document_id,
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

                    version_id = uuid.uuid4()
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
                        "document_id": str(document_id),
                        "version_id": str(version_id),
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
            "Uploaded markdown: org=%s doc=%s version=%s size=%s hash=%s uploaded_object=%s",
            organization_id,
            document_id,
            version_no,
            size_bytes,
            content_hash,
            uploaded_object,
        )

        return {
            "document_id": document_id,
            "version_id": version_id,
            "version_no": version_no,
            "content_hash": content_hash,
            "object_key": object_key,
            "size_bytes": size_bytes,
        }

    def list_documents(self, *, organization_id: uuid.UUID, limit: int, offset: int) -> list[dict[str, Any]]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
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
                        v.parent_version_id
                    FROM documents d
                    LEFT JOIN document_versions v
                           ON v.id = d.current_version_id
                          AND v.organization_id = d.organization_id
                    WHERE d.organization_id = %s
                    ORDER BY d.updated_at DESC
                    LIMIT %s
                    OFFSET %s;
                    """,
                    (organization_id, limit, offset),
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
                }
            )
        return items

    def _get_version_by_number(
        self,
        cur: psycopg.Cursor[Any],
        *,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
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
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        version_id: uuid.UUID,
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
        organization_id: uuid.UUID,
        actor_user_id: str | None,
        document_id: uuid.UUID,
        metadata: dict[str, Any],
    ) -> None:
        if not actor_user_id:
            return

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
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                document_row = self._assert_document_in_org(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
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
                "document_id": str(document_id),
                "version_id": str(version["version_id"]),
                "version_no": version["version_no"],
                "content_hash": version["content_hash"],
                "size_bytes": version["size_bytes"],
            },
        )

        return {
            "document": self._map_document_row(document_row),
            "version": version,
            "content_bytes": content_bytes,
        }

    def get_document_version(
        self,
        *,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        version_no: int,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                document_row = self._assert_document_in_org(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
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
                "document_id": str(document_id),
                "version_id": str(version["version_id"]),
                "version_no": version["version_no"],
                "content_hash": version["content_hash"],
                "size_bytes": version["size_bytes"],
            },
        )

        return {
            "document": self._map_document_row(document_row),
            "version": version,
            "content_bytes": content_bytes,
        }

    def list_versions(
        self,
        *,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        with psycopg.connect(self.settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                self._assert_document_in_org(
                    cur,
                    organization_id=organization_id,
                    document_id=document_id,
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
