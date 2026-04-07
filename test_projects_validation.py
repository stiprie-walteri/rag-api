import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.routes.projects import CreateProjectRequest, create_project
from app.services.storage.service import DocumentStorageService, DocumentStorageSettings


class _FakeAsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeCursor:
    def __init__(self, fetchone_results=None):
        self.fetchone_results = list(fetchone_results or [])
        self.executed = []
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.executed.append((normalized, params))
        return None

    async def fetchone(self):
        if self.fetchone_results:
            return self.fetchone_results.pop(0)
        return None

    async def fetchall(self):
        return []


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def transaction(self):
        return _FakeAsyncContext(None)

    def cursor(self):
        return self._cursor


def _settings():
    return DocumentStorageSettings(
        postgres_dsn="postgresql://unused",
        minio_endpoint="localhost:9000",
        minio_access_key="minioadmin",
        minio_secret_key="minioadmin",
        minio_secure=False,
        minio_bucket="docstore",
        migrations_dir=Path("migrations"),
    )


class ProjectStorageValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_project_inserts_folder(self):
        service = DocumentStorageService(_settings())
        cursor = _FakeCursor(
            fetchone_results=[
                {
                    "id": "project-1",
                    "organization_id": "org-1",
                    "name": "Folder A",
                    "description": "Docs for one project folder",
                    "legislation_template_ids": ["mica-programme-of-operations-highlight-v4"],
                    "created_at": "2026-04-07T12:00:00Z",
                    "created_by": "user-1",
                    "updated_at": "2026-04-07T12:00:00Z",
                    "updated_by": "user-1",
                    "document_count": 0,
                }
            ]
        )
        connection = _FakeConnection(cursor)

        with patch("app.services.storage.service.psycopg.AsyncConnection.connect", new=AsyncMock(return_value=connection)):
            with patch.object(service, "_ensure_user_in_organization", new=AsyncMock()):
                result = await service.create_project(
                    organization_id="org-1",
                    name="Folder A",
                    description="Docs for one project folder",
                    legislation_template_ids=["mica-programme-of-operations-highlight-v4"],
                    actor_user_id="user-1",
                )

        self.assertEqual(result["project_id"], "project-1")
        self.assertEqual(
            result["legislation_template_ids"],
            ["mica-programme-of-operations-highlight-v4"],
        )
        self.assertTrue(any("INSERT INTO folders" in query for query, _ in cursor.executed))

    async def test_map_project_row_includes_empty_legislation_ids(self):
        result = DocumentStorageService._map_project_row(
            {
                "id": "project-2",
                "organization_id": "org-1",
                "name": "General",
                "description": None,
                "legislation_template_ids": None,
                "created_at": "2026-04-07T12:00:00Z",
                "created_by": "user-1",
                "updated_at": "2026-04-07T12:00:00Z",
                "updated_by": "user-1",
                "document_count": 3,
            }
        )

        self.assertEqual(result["legislation_template_ids"], [])

    async def test_create_project_route_requires_legislation_selection(self):
        with patch("app.api.routes.projects.require_docstore") as require_docstore_mock:
            service = object()
            require_docstore_mock.return_value = service
            with patch("app.api.routes.projects.validate_template_ids", return_value=[]):
                with patch(
                    "app.api.routes.projects.sync_authenticated_org",
                    new=AsyncMock(return_value={"organization_id": "org-1"}),
                ):
                    with self.assertRaises(HTTPException) as exc:
                        await create_project(
                            organization_id="org-1",
                            body=CreateProjectRequest(name="Project A", legislation_template_ids=[]),
                            auth=type("Auth", (), {"user_id": "user-1"})(),
                        )

        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("At least one legislation template", exc.exception.detail)

    async def test_multiple_new_documents_can_share_one_project(self):
        service = DocumentStorageService(_settings())
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)

        with patch("app.services.storage.service.psycopg.AsyncConnection.connect", new=AsyncMock(return_value=connection)):
            with patch.object(service, "minio_put_if_missing", new=AsyncMock(return_value=True)):
                with patch.object(service, "_ensure_user", new=AsyncMock()):
                    with patch.object(service, "_ensure_user_in_organization", new=AsyncMock()):
                        with patch.object(service, "_assert_project_in_org", new=AsyncMock()):
                            with patch.object(service, "_upsert_document_membership", new=AsyncMock()):
                                with patch.object(service, "_write_audit", new=AsyncMock()):
                                    first = await service.create_version(
                                        organization_id="org-1",
                                        actor_user_id="user-1",
                                        markdown_bytes=b"# first",
                                        project_id="project-1",
                                        title="First doc",
                                    )
                                    second = await service.create_version(
                                        organization_id="org-1",
                                        actor_user_id="user-1",
                                        markdown_bytes=b"# second",
                                        project_id="project-1",
                                        title="Second doc",
                                    )

        self.assertEqual(first["project_id"], "project-1")
        self.assertEqual(second["project_id"], "project-1")

        document_insert_params = [
            params
            for query, params in cursor.executed
            if "INSERT INTO documents" in query
        ]
        self.assertEqual(len(document_insert_params), 2)
        self.assertEqual(document_insert_params[0][3], "project-1")
        self.assertEqual(document_insert_params[1][3], "project-1")
        self.assertNotEqual(first["document_id"], second["document_id"])


if __name__ == "__main__":
    unittest.main()
