import asyncio
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.dependencies import require_docstore, sync_authenticated_org
from app.api.routes.documents import DocumentListResponse, _to_document_list_item
from app.core.auth import AuthContext, get_auth_context
from app.services.evaluation import state as eval_state
from app.services.evaluation.agent import TaskEvaluationResult, evaluate_task_with_agent
from app.services.legislation.templates import get_legislation_template, validate_template_ids
from app.services.storage.service import (
    DocumentAccessDeniedError,
    DocumentNotFoundError,
    DocumentOperationConflictError,
    OrganizationMismatchError,
    ProjectNotFoundError,
)

router = APIRouter(tags=["Projects"])


class ProjectItem(BaseModel):
    project_id: str
    organization_id: str
    name: str
    description: str | None = None
    legislation_template_ids: list[str] = []
    created_at: datetime
    created_by: str
    updated_at: datetime
    updated_by: str
    document_count: int = 0


class CreateProjectRequest(BaseModel):
    name: str
    description: str | None = None
    legislation_template_ids: list[str]


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    legislation_template_ids: list[str] | None = None


class MoveDocumentToProjectRequest(BaseModel):
    project_id: str


class ProjectEvaluationRequest(BaseModel):
    template_ids: list[str] | None = None


class ProjectEvaluationStartedResponse(BaseModel):
    job_id: str
    project_id: str
    status: str = "running"
    status_message: str | None = None


class ProjectEvaluationActivityItem(BaseModel):
    at: datetime
    phase: str
    message: str


class ProjectEvaluationStatusResponse(BaseModel):
    job_id: str
    status: str
    organization_id: str
    project_id: str
    total_tasks: int
    completed_count: int
    progress_percent: int = 0
    status_message: str | None = None
    estimated_seconds_remaining: int | None = None
    estimated_completion_at: datetime | None = None
    current_task: list[str] | None = None
    results: list[TaskEvaluationResult]
    compliance_result: dict | None = None
    error: str | None = None
    started_at: datetime
    updated_at: datetime
    activity: list[ProjectEvaluationActivityItem] = []
    documents: list[dict] = []
    legislation_template_ids: list[str] = []


class ProjectComplianceResponse(BaseModel):
    project_id: str
    compliance_result: dict | None = None


def _project_target_key(project_id: str) -> str:
    return f"project:{project_id}"


def _build_template_run(template_data: dict) -> dict:
    return {
        "template_id": template_data["id"],
        "template_name": template_data["name"],
        "tasks": template_data.get("Tasks") or [],
        "system_prompt_override": template_data.get("system_prompt"),
        "references": template_data.get("references"),
    }


async def _run_project_evaluation_background(
    *,
    job_id: str,
    project_id: str,
    organization_id: str,
    evaluation_runs: list[dict],
    chunks_raw: list[dict],
    documents: list[dict],
    service,
) -> None:
    target_key = _project_target_key(project_id)
    try:
        await eval_state.update_progress(
            job_id,
            current_task=None,
            completed_count=0,
            status_message=f"Preparing combined project corpus from {len(documents)} documents.",
            activity_message=(
                f"Prepared analysis context from {len(documents)} documents and {len(chunks_raw)} extracted sections."
            ),
            phase="setup",
        )
        total_tasks = sum(len(run["tasks"]) for run in evaluation_runs)
        results: list[TaskEvaluationResult | None] = [None] * total_tasks
        completed_count = 0
        state_lock = asyncio.Lock()
        run_summaries: list[dict] = []
        scheduled_runs: list[tuple[int, dict, list[str]]] = []
        result_index = 0

        for run in evaluation_runs:
            run_result_start = result_index
            for task_list in run["tasks"]:
                scheduled_runs.append((result_index, run, task_list))
                result_index += 1
            run_summaries.append(
                {
                    "template_id": run["template_id"],
                    "template_name": run["template_name"],
                    "task_count": len(run["tasks"]),
                    "result_indexes": list(range(run_result_start, result_index)),
                }
            )

        async def _run_one(idx: int, run: dict, task_list: list[str]) -> None:
            nonlocal completed_count
            async with state_lock:
                await eval_state.update_progress(
                    job_id,
                    current_task=task_list,
                    completed_count=completed_count,
                    status_message=f"Running {run['template_name']}: {task_list[0]}",
                    activity_message=f"Started {run['template_name']} task: {task_list[0]}",
                    phase="task_start",
                )
            result = await evaluate_task_with_agent(
                task_list=task_list,
                chunks=chunks_raw,
                system_prompt_override=run.get("system_prompt_override"),
                references=run.get("references"),
            )
            result.legislation_id = run["template_id"]
            result.legislation_name = run["template_name"]
            results[idx] = result
            async with state_lock:
                completed_count += 1
                await eval_state.append_result(
                    job_id,
                    result=result.model_dump(),
                    completed_count=completed_count,
                    status_message=f"Completed {completed_count} of {total_tasks} tasks.",
                    activity_message=(
                        f"Finished {run['template_name']} task: {task_list[0]} "
                        f"({'passed' if result.exists else 'issues found'})."
                    ),
                    phase="task_complete",
                )

        await asyncio.gather(*(_run_one(i, run, task_list) for i, run, task_list in scheduled_runs))

        await eval_state.update_progress(
            job_id,
            current_task=None,
            completed_count=completed_count,
            status_message="Finalizing project report.",
            activity_message="All tasks finished. Building grouped legislation output.",
            phase="finalize",
        )

        final_results = [r.model_dump() for r in results if r is not None]
        compliance_result = {
            "evaluation_job_id": job_id,
            "project_id": project_id,
            "documents": documents,
            "legislation_template_ids": [run["template_id"] for run in evaluation_runs],
            "legislations": [
                {
                    "template_id": run_summary["template_id"],
                    "template_name": run_summary["template_name"],
                    "task_count": run_summary["task_count"],
                    "results": [
                        results[idx].model_dump()
                        for idx in run_summary["result_indexes"]
                        if results[idx] is not None
                    ],
                }
                for run_summary in run_summaries
            ],
            "results": final_results,
        }

        await service.save_project_compliance_result(
            organization_id=organization_id,
            project_id=project_id,
            result=compliance_result,
        )
        await eval_state.complete_job(job_id, status_message="Project analysis complete.")
    except Exception as exc:
        await eval_state.fail_job(job_id, str(exc), status_message="Project analysis failed.")
    finally:
        await eval_state.release_lock(target_key)


@router.post("/api/orgs/{organization_id}/projects", response_model=ProjectItem, status_code=201)
async def create_project(
    organization_id: str,
    body: CreateProjectRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    try:
        legislation_template_ids = validate_template_ids(body.legislation_template_ids)
        if not legislation_template_ids:
            raise ValueError("At least one legislation template must be selected when creating a project.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        row = await service.create_project(
            organization_id=org_context["organization_id"],
            name=body.name,
            description=body.description,
            legislation_template_ids=legislation_template_ids,
            actor_user_id=auth.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ProjectItem(**row)


@router.get("/api/orgs/{organization_id}/projects", response_model=list[ProjectItem])
async def list_projects(
    organization_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    rows = await service.list_projects(
        organization_id=org_context["organization_id"],
        actor_user_id=auth.user_id,
    )
    return [ProjectItem(**row) for row in rows]


@router.get("/api/orgs/{organization_id}/projects/{project_id}", response_model=ProjectItem)
async def get_project(
    organization_id: str,
    project_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        row = await service.get_project(
            organization_id=org_context["organization_id"],
            project_id=project_id,
            actor_user_id=auth.user_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return ProjectItem(**row)


@router.patch("/api/orgs/{organization_id}/projects/{project_id}", response_model=ProjectItem)
async def update_project(
    organization_id: str,
    project_id: str,
    body: UpdateProjectRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    try:
        legislation_template_ids = (
            validate_template_ids(body.legislation_template_ids)
            if body.legislation_template_ids is not None
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        row = await service.update_project(
            organization_id=org_context["organization_id"],
            project_id=project_id,
            actor_user_id=auth.user_id,
            name=body.name,
            description=body.description,
            legislation_template_ids=legislation_template_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return ProjectItem(**row)


@router.delete("/api/orgs/{organization_id}/projects/{project_id}", status_code=204)
async def delete_project(
    organization_id: str,
    project_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        await service.delete_project(
            organization_id=org_context["organization_id"],
            project_id=project_id,
            actor_user_id=auth.user_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DocumentOperationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get(
    "/api/orgs/{organization_id}/projects/{project_id}/documents",
    response_model=DocumentListResponse,
)
async def list_project_documents(
    organization_id: str,
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        items_raw = await service.list_documents(
            organization_id=org_context["organization_id"],
            actor_user_id=auth.user_id,
            limit=limit,
            offset=offset,
            project_id=project_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    items = [_to_document_list_item(item) for item in items_raw]
    next_offset = offset + len(items) if len(items) == limit else None
    return DocumentListResponse(items=items, limit=limit, offset=offset, next_offset=next_offset)


@router.post(
    "/api/orgs/{organization_id}/projects/{project_id}/evaluate",
    response_model=ProjectEvaluationStartedResponse,
    status_code=202,
)
async def evaluate_project(
    organization_id: str,
    project_id: str,
    request: ProjectEvaluationRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )

    target_key = _project_target_key(project_id)
    if await eval_state.is_locked(target_key):
        raise HTTPException(status_code=409, detail=f"An evaluation is already running for project {project_id}.")

    try:
        context = await service.get_project_evaluation_context(
            organization_id=org_context["organization_id"],
            project_id=project_id,
            actor_user_id=auth.user_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OrganizationMismatchError, DocumentAccessDeniedError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    template_ids = request.template_ids
    if template_ids is None:
        template_ids = context["project"].get("legislation_template_ids") or []

    try:
        template_ids = validate_template_ids(template_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not template_ids:
        raise HTTPException(status_code=400, detail="Project has no legislation templates configured.")
    if not context["documents"]:
        raise HTTPException(status_code=400, detail="Project has no documents to evaluate.")
    if not context["chunks"]:
        raise HTTPException(status_code=400, detail="Project documents have no extracted content to evaluate.")

    evaluation_runs: list[dict] = []
    for template_id in template_ids:
        template_data = get_legislation_template(template_id)
        if not template_data:
            raise HTTPException(status_code=400, detail=f"Project references unknown legislation template {template_id}.")
        evaluation_runs.append(_build_template_run(template_data))

    total_tasks = sum(len(run["tasks"]) for run in evaluation_runs)
    if total_tasks == 0:
        raise HTTPException(status_code=400, detail="No tasks found in the selected legislation templates.")

    job_id = str(uuid.uuid4())
    if not await eval_state.acquire_lock(target_key, job_id):
        raise HTTPException(status_code=409, detail=f"An evaluation is already running for project {project_id}.")

    await eval_state.start_job(
        job_id=job_id,
        target_key=target_key,
        organization_id=org_context["organization_id"],
        target_type="project",
        target_id=project_id,
        version_id=None,
        total_tasks=total_tasks,
        metadata={
            "documents": context["documents"],
            "legislation_template_ids": template_ids,
        },
        status_message="Queued project analysis.",
    )

    asyncio.create_task(
        _run_project_evaluation_background(
            job_id=job_id,
            project_id=project_id,
            organization_id=org_context["organization_id"],
            evaluation_runs=evaluation_runs,
            chunks_raw=context["chunks"],
            documents=context["documents"],
            service=service,
        )
    )

    return ProjectEvaluationStartedResponse(
        job_id=job_id,
        project_id=project_id,
        status="running",
        status_message="Queued project analysis.",
    )


@router.get(
    "/api/orgs/{organization_id}/projects/{project_id}/evaluation/status",
    response_model=ProjectEvaluationStatusResponse,
)
async def get_project_evaluation_status(
    organization_id: str,
    project_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )

    try:
        await service.get_project(
            organization_id=org_context["organization_id"],
            project_id=project_id,
            actor_user_id=auth.user_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    job_id = await eval_state.get_current_job_id(_project_target_key(project_id))
    if not job_id:
        raise HTTPException(status_code=404, detail="No project evaluation found.")

    job = await eval_state.get_job_state(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Project evaluation state has expired.")
    if job["organization_id"] != org_context["organization_id"]:
        raise HTTPException(status_code=403, detail="Access denied.")

    compliance_result = await service.get_project_compliance_result(
        organization_id=org_context["organization_id"],
        project_id=project_id,
        actor_user_id=auth.user_id,
    )
    metadata = job.get("metadata") or {}

    return ProjectEvaluationStatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        organization_id=job["organization_id"],
        project_id=project_id,
        total_tasks=job["total_tasks"],
        completed_count=job["completed_count"],
        progress_percent=job.get("progress_percent", 0),
        status_message=job.get("status_message"),
        estimated_seconds_remaining=job.get("estimated_seconds_remaining"),
        estimated_completion_at=job.get("estimated_completion_at"),
        current_task=job.get("current_task"),
        results=[TaskEvaluationResult(**r) for r in job.get("results", [])],
        compliance_result=compliance_result,
        error=job.get("error"),
        started_at=job["started_at"],
        updated_at=job["updated_at"],
        activity=[
            ProjectEvaluationActivityItem(**item) for item in (job.get("activity") or [])
        ],
        documents=metadata.get("documents") or [],
        legislation_template_ids=metadata.get("legislation_template_ids") or [],
    )


@router.get(
    "/api/orgs/{organization_id}/projects/{project_id}/compliance",
    response_model=ProjectComplianceResponse,
)
async def get_project_compliance_result(
    organization_id: str,
    project_id: str,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        compliance_result = await service.get_project_compliance_result(
            organization_id=org_context["organization_id"],
            project_id=project_id,
            actor_user_id=auth.user_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrganizationMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return ProjectComplianceResponse(project_id=project_id, compliance_result=compliance_result)


@router.patch("/api/orgs/{organization_id}/documents/{document_id}/project", status_code=204)
async def move_document_to_project(
    organization_id: str,
    document_id: str,
    body: MoveDocumentToProjectRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    service = require_docstore()
    org_context = await sync_authenticated_org(
        service, auth, requested_organization_id=organization_id, create_if_missing=False
    )
    try:
        await service.move_document_to_project(
            document_id=document_id,
            organization_id=org_context["organization_id"],
            project_id=body.project_id,
            actor_user_id=auth.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (DocumentNotFoundError, ProjectNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (DocumentAccessDeniedError, OrganizationMismatchError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
