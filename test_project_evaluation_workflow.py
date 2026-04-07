import asyncio
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.api.routes.projects import evaluate_project, get_project_evaluation_status
from app.services.evaluation.agent import TaskEvaluationResult


class _Auth:
    def __init__(self, user_id: str):
        self.user_id = user_id


class _FakeProjectService:
    def __init__(self, project_context: dict):
        self.project_context = project_context
        self.saved_compliance_result = None

    async def get_project_evaluation_context(self, *, organization_id: str, project_id: str, actor_user_id: str):
        return self.project_context

    async def save_project_compliance_result(self, *, organization_id: str, project_id: str, result: dict):
        self.saved_compliance_result = result

    async def get_project(self, *, organization_id: str, project_id: str, actor_user_id: str):
        return self.project_context["project"]

    async def get_project_compliance_result(self, *, organization_id: str, project_id: str, actor_user_id: str):
        return self.saved_compliance_result


class _InMemoryEvalState:
    def __init__(self):
        self.locks: dict[str, str] = {}
        self.current_jobs: dict[str, str] = {}
        self.jobs: dict[str, dict] = {}

    async def is_locked(self, target_key: str) -> bool:
        return target_key in self.locks

    async def acquire_lock(self, target_key: str, job_id: str) -> bool:
        if target_key in self.locks:
            return False
        self.locks[target_key] = job_id
        return True

    async def release_lock(self, target_key: str) -> None:
        self.locks.pop(target_key, None)

    async def start_job(
        self,
        *,
        job_id: str,
        target_key: str,
        organization_id: str,
        target_type: str,
        target_id: str,
        version_id: str | None,
        total_tasks: int,
        metadata: dict | None = None,
        status_message: str | None = None,
    ) -> None:
        self.current_jobs[target_key] = job_id
        self.jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "organization_id": organization_id,
            "target_type": target_type,
            "target_id": target_id,
            "project_id": target_id if target_type == "project" else None,
            "document_id": target_id if target_type == "document" else None,
            "version_id": version_id,
            "total_tasks": total_tasks,
            "completed_count": 0,
            "progress_percent": 0,
            "current_task": None,
            "status_message": status_message,
            "estimated_seconds_remaining": None,
            "estimated_completion_at": None,
            "activity": [],
            "results": [],
            "error": None,
            "started_at": "2026-04-07T12:00:00Z",
            "updated_at": "2026-04-07T12:00:00Z",
            "metadata": metadata or {},
        }

    async def update_progress(
        self,
        job_id: str,
        *,
        current_task: list[str] | None,
        completed_count: int,
        status_message: str | None = None,
        activity_message: str | None = None,
        phase: str = "progress",
    ) -> None:
        self.jobs[job_id]["current_task"] = current_task
        self.jobs[job_id]["completed_count"] = completed_count
        total_tasks = max(1, self.jobs[job_id]["total_tasks"])
        self.jobs[job_id]["progress_percent"] = int((completed_count / total_tasks) * 100)
        remaining = max(0, total_tasks - completed_count)
        self.jobs[job_id]["estimated_seconds_remaining"] = remaining
        self.jobs[job_id]["estimated_completion_at"] = "2026-04-07T12:10:00Z"
        if status_message is not None:
            self.jobs[job_id]["status_message"] = status_message
        if activity_message:
            self.jobs[job_id]["activity"].append(
                {"at": "2026-04-07T12:00:00Z", "phase": phase, "message": activity_message}
            )

    async def append_result(
        self,
        job_id: str,
        *,
        result: dict,
        completed_count: int,
        status_message: str | None = None,
        activity_message: str | None = None,
        phase: str = "progress",
    ) -> None:
        self.jobs[job_id]["results"].append(result)
        self.jobs[job_id]["completed_count"] = completed_count
        self.jobs[job_id]["current_task"] = None
        total_tasks = max(1, self.jobs[job_id]["total_tasks"])
        self.jobs[job_id]["progress_percent"] = int((completed_count / total_tasks) * 100)
        remaining = max(0, total_tasks - completed_count)
        self.jobs[job_id]["estimated_seconds_remaining"] = remaining
        self.jobs[job_id]["estimated_completion_at"] = "2026-04-07T12:10:00Z"
        if status_message is not None:
            self.jobs[job_id]["status_message"] = status_message
        if activity_message:
            self.jobs[job_id]["activity"].append(
                {"at": "2026-04-07T12:00:00Z", "phase": phase, "message": activity_message}
            )

    async def complete_job(self, job_id: str, *, status_message: str | None = None) -> None:
        self.jobs[job_id]["status"] = "completed"
        self.jobs[job_id]["progress_percent"] = 100
        self.jobs[job_id]["status_message"] = status_message
        self.jobs[job_id]["estimated_seconds_remaining"] = 0
        self.jobs[job_id]["estimated_completion_at"] = "2026-04-07T12:10:00Z"

    async def fail_job(self, job_id: str, error: str, *, status_message: str | None = None) -> None:
        self.jobs[job_id]["status"] = "failed"
        self.jobs[job_id]["error"] = error
        self.jobs[job_id]["status_message"] = status_message

    async def get_current_job_id(self, target_key: str) -> str | None:
        return self.current_jobs.get(target_key)

    async def get_job_state(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)


class ProjectEvaluationWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_evaluation_uses_multiple_documents_and_multiple_legislations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            templates_dir = Path(tmpdir)
            (templates_dir / "leg_alpha.yaml").write_text(
                textwrap.dedent(
                    """
                    id: leg-alpha
                    name: Legislation Alpha
                    Tasks:
                      - ["Check alpha and beta across the project"]
                    """
                ).strip(),
                encoding="utf-8",
            )
            (templates_dir / "leg_gamma.yaml").write_text(
                textwrap.dedent(
                    """
                    id: leg-gamma
                    name: Legislation Gamma
                    Tasks:
                      - ["Check gamma and delta across the project"]
                    """
                ).strip(),
                encoding="utf-8",
            )

            project_context = {
                "project": {
                    "project_id": "project-1",
                    "organization_id": "org-1",
                    "name": "Synthetic Verification Project",
                    "description": "Project-level evaluation verification",
                    "legislation_template_ids": ["leg-alpha", "leg-gamma"],
                    "created_at": "2026-04-07T12:00:00Z",
                    "created_by": "user-1",
                    "updated_at": "2026-04-07T12:00:00Z",
                    "updated_by": "user-1",
                    "document_count": 2,
                },
                "documents": [
                    {
                        "document_id": "doc-1",
                        "title": "Operations Overview",
                        "version_id": "ver-1",
                        "version_no": 1,
                    },
                    {
                        "document_id": "doc-2",
                        "title": "Governance Annex",
                        "version_id": "ver-2",
                        "version_no": 1,
                    },
                ],
                "chunks": [
                    {
                        "id": "chunk-1",
                        "organization_id": "org-1",
                        "document_id": "doc-1",
                        "version_id": "ver-1",
                        "chunk_level": 1,
                        "title": "Operations Overview :: Scope",
                        "start_page": 1,
                        "end_page": 1,
                        "text_content": "This document contains alpha controls and delta governance.",
                    },
                    {
                        "id": "chunk-2",
                        "organization_id": "org-1",
                        "document_id": "doc-2",
                        "version_id": "ver-2",
                        "chunk_level": 1,
                        "title": "Governance Annex :: Risk",
                        "start_page": 1,
                        "end_page": 1,
                        "text_content": "This document contains beta evidence and gamma oversight.",
                    },
                ],
            }
            service = _FakeProjectService(project_context)
            eval_state = _InMemoryEvalState()
            scheduled_tasks: list[asyncio.Task] = []
            real_create_task = asyncio.create_task

            async def fake_evaluate_task_with_agent(task_list, chunks, system_prompt_override=None, references=None):
                corpus = " ".join(chunk["text_content"].lower() for chunk in chunks)
                task_text = " ".join(task_list).lower()
                if "alpha and beta" in task_text:
                    exists = "alpha" in corpus and "beta" in corpus
                elif "gamma and delta" in task_text:
                    exists = "gamma" in corpus and "delta" in corpus
                else:
                    exists = False
                return TaskEvaluationResult(
                    task=task_list,
                    exists=exists,
                    explanation=f"Verified against combined corpus of {len(chunks)} chunks.",
                    missing_sections=[],
                    incorrect_sections=[],
                    reasoning_steps=[],
                )

            def capture_create_task(coro):
                task = real_create_task(coro)
                scheduled_tasks.append(task)
                return task

            with patch.dict(os.environ, {"LEGISLATION_TEMPLATES_DIR": str(templates_dir)}):
                with patch("app.api.routes.projects.require_docstore", return_value=service):
                    with patch(
                        "app.api.routes.projects.sync_authenticated_org",
                        new=AsyncMock(return_value={"organization_id": "org-1"}),
                    ):
                        with patch("app.api.routes.projects.eval_state", eval_state):
                            with patch(
                                "app.api.routes.projects.evaluate_task_with_agent",
                                new=AsyncMock(side_effect=fake_evaluate_task_with_agent),
                            ):
                                with patch("app.api.routes.projects.asyncio.create_task", side_effect=capture_create_task):
                                    start_response = await evaluate_project(
                                        organization_id="org-1",
                                        project_id="project-1",
                                        request=type("Req", (), {"template_ids": None})(),
                                        auth=_Auth("user-1"),
                                    )

                                    self.assertEqual(start_response.project_id, "project-1")
                                    self.assertEqual(start_response.status, "running")
                                    self.assertEqual(len(scheduled_tasks), 1)

                                    await scheduled_tasks[0]

                                    status_response = await get_project_evaluation_status(
                                        organization_id="org-1",
                                        project_id="project-1",
                                        auth=_Auth("user-1"),
                                    )

            self.assertEqual(status_response.status, "completed")
            self.assertEqual(status_response.total_tasks, 2)
            self.assertEqual(status_response.completed_count, 2)
            self.assertEqual(status_response.progress_percent, 100)
            self.assertEqual(status_response.status_message, "Project analysis complete.")
            self.assertEqual(status_response.estimated_seconds_remaining, 0)
            self.assertIsNotNone(status_response.estimated_completion_at)
            self.assertEqual(len(status_response.documents), 2)
            self.assertEqual(status_response.legislation_template_ids, ["leg-alpha", "leg-gamma"])
            self.assertEqual(len(status_response.results), 2)
            self.assertTrue(all(result.exists for result in status_response.results))
            self.assertGreaterEqual(len(status_response.activity), 3)

            saved = service.saved_compliance_result
            self.assertIsNotNone(saved)
            self.assertEqual(saved["project_id"], "project-1")
            self.assertEqual(len(saved["documents"]), 2)
            self.assertEqual(saved["legislation_template_ids"], ["leg-alpha", "leg-gamma"])
            self.assertEqual(len(saved["legislations"]), 2)
            self.assertEqual(saved["legislations"][0]["template_id"], "leg-alpha")
            self.assertEqual(saved["legislations"][1]["template_id"], "leg-gamma")
            self.assertTrue(all(item["results"][0]["exists"] for item in saved["legislations"]))


if __name__ == "__main__":
    unittest.main()
