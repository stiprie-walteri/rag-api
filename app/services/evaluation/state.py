import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 3600
JOB_TTL_SECONDS = 86400
CURRENT_TTL_SECONDS = 90000

_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    global _client
    if _client is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _client = redis.from_url(url, decode_responses=True)
    return _client


def _lock_key(target_key: str) -> str:
    return f"eval:lock:{target_key}"


def _job_key(job_id: str) -> str:
    return f"eval:job:{job_id}"


def _current_key(target_key: str) -> str:
    return f"eval:current:{target_key}"


def _compute_progress_percent(*, completed_count: int, total_tasks: int) -> int:
    if total_tasks <= 0:
        return 0
    return min(100, max(0, int((completed_count / total_tasks) * 100)))


def _default_eta() -> tuple[int, str]:
    now = datetime.now(timezone.utc)
    return 3600, (now + timedelta(hours=1)).isoformat()


def _compute_eta(state: dict[str, Any]) -> tuple[int | None, str | None]:
    total_tasks = int(state.get("total_tasks") or 0)
    completed_count = int(state.get("completed_count") or 0)
    started_at_raw = state.get("started_at")
    if total_tasks <= 0 or completed_count <= 0 or not started_at_raw:
        return None, None

    try:
        started_at = datetime.fromisoformat(started_at_raw)
    except ValueError:
        return None, None

    now = datetime.now(timezone.utc)
    elapsed_seconds = max(0, int((now - started_at).total_seconds()))
    if elapsed_seconds <= 0:
        return None, None

    avg_seconds_per_task = elapsed_seconds / completed_count
    remaining_tasks = max(0, total_tasks - completed_count)
    eta_seconds = int(avg_seconds_per_task * remaining_tasks)
    return eta_seconds, (now + timedelta(seconds=eta_seconds)).isoformat()


def _append_activity(
    state: dict[str, Any],
    *,
    message: str,
    phase: str = "info",
    max_items: int = 50,
) -> None:
    activity = list(state.get("activity") or [])
    activity.append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "message": message,
        }
    )
    state["activity"] = activity[-max_items:]


async def acquire_lock(target_key: str, job_id: str) -> bool:
    client = get_redis_client()
    result = await client.set(_lock_key(target_key), job_id, nx=True, ex=LOCK_TTL_SECONDS)
    return result is not None


async def release_lock(target_key: str) -> None:
    client = get_redis_client()
    await client.delete(_lock_key(target_key))


async def is_locked(target_key: str) -> bool:
    client = get_redis_client()
    return await client.exists(_lock_key(target_key)) == 1


async def start_job(
    *,
    job_id: str,
    target_key: str,
    organization_id: str,
    target_type: str,
    target_id: str,
    version_id: str | None,
    total_tasks: int,
    metadata: dict[str, Any] | None = None,
    status_message: str | None = None,
) -> None:
    client = get_redis_client()
    now = datetime.now(timezone.utc).isoformat()
    default_eta_seconds, default_eta_at = _default_eta()
    state: dict[str, Any] = {
        "job_id": job_id,
        "status": "running",
        "organization_id": organization_id,
        "target_type": target_type,
        "target_id": target_id,
        "document_id": target_id if target_type == "document" else None,
        "project_id": target_id if target_type == "project" else None,
        "version_id": version_id,
        "total_tasks": total_tasks,
        "completed_count": 0,
        "current_task": None,
        "results": [],
        "error": None,
        "status_message": status_message or "Starting analysis.",
        "progress_percent": 0,
        "estimated_seconds_remaining": default_eta_seconds,
        "estimated_completion_at": default_eta_at,
        "activity": [],
        "started_at": now,
        "updated_at": now,
        "metadata": metadata or {},
    }
    _append_activity(state, message=state["status_message"], phase="start")
    pipe = client.pipeline()
    pipe.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)
    pipe.set(_current_key(target_key), job_id, ex=CURRENT_TTL_SECONDS)
    await pipe.execute()


async def update_progress(
    job_id: str,
    *,
    current_task: list[str] | None,
    completed_count: int,
    status_message: str | None = None,
    activity_message: str | None = None,
    phase: str = "progress",
) -> None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    if not raw:
        return
    state = json.loads(raw)
    state["current_task"] = current_task
    state["completed_count"] = completed_count
    state["progress_percent"] = _compute_progress_percent(
        completed_count=completed_count,
        total_tasks=int(state.get("total_tasks") or 0),
    )
    eta_seconds, eta_at = _compute_eta(state)
    if eta_seconds is None or eta_at is None:
        eta_seconds, eta_at = _default_eta()
    state["estimated_seconds_remaining"] = eta_seconds
    state["estimated_completion_at"] = eta_at
    if status_message is not None:
        state["status_message"] = status_message
    if activity_message:
        _append_activity(state, message=activity_message, phase=phase)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    await client.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


async def append_result(
    job_id: str,
    *,
    result: dict[str, Any],
    completed_count: int,
    status_message: str | None = None,
    activity_message: str | None = None,
    phase: str = "progress",
) -> None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    if not raw:
        return
    state = json.loads(raw)
    state["results"].append(result)
    state["completed_count"] = completed_count
    state["current_task"] = None
    state["progress_percent"] = _compute_progress_percent(
        completed_count=completed_count,
        total_tasks=int(state.get("total_tasks") or 0),
    )
    eta_seconds, eta_at = _compute_eta(state)
    if eta_seconds is None or eta_at is None:
        eta_seconds, eta_at = _default_eta()
    state["estimated_seconds_remaining"] = eta_seconds
    state["estimated_completion_at"] = eta_at
    if status_message is not None:
        state["status_message"] = status_message
    if activity_message:
        _append_activity(state, message=activity_message, phase=phase)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    await client.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


async def complete_job(job_id: str, *, status_message: str | None = None) -> None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    if not raw:
        return
    state = json.loads(raw)
    state["status"] = "completed"
    state["current_task"] = None
    state["completed_count"] = int(state.get("total_tasks") or state.get("completed_count") or 0)
    state["progress_percent"] = 100
    state["status_message"] = status_message or "Analysis complete."
    state["estimated_seconds_remaining"] = 0
    state["estimated_completion_at"] = datetime.now(timezone.utc).isoformat()
    _append_activity(state, message=state["status_message"], phase="complete")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    await client.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


async def fail_job(job_id: str, error: str, *, status_message: str | None = None) -> None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    if not raw:
        return
    state = json.loads(raw)
    state["status"] = "failed"
    state["error"] = error
    state["current_task"] = None
    state["status_message"] = status_message or "Analysis failed."
    default_eta_seconds, default_eta_at = _default_eta()
    state["estimated_seconds_remaining"] = default_eta_seconds
    state["estimated_completion_at"] = default_eta_at
    _append_activity(state, message=f"{state['status_message']} {error}", phase="error")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    await client.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


async def get_job_state(job_id: str) -> dict[str, Any] | None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    return json.loads(raw) if raw else None


async def get_current_job_id(target_key: str) -> str | None:
    client = get_redis_client()
    return await client.get(_current_key(target_key))


async def get_job_states_for_documents(document_ids: list[str]) -> dict[str, dict[str, Any] | None]:
    if not document_ids:
        return {}

    client = get_redis_client()
    pipe = client.pipeline()
    for doc_id in document_ids:
        pipe.get(_current_key(doc_id))
    current_job_ids: list[str | None] = await pipe.execute()

    job_states: dict[str, Any] = {}
    job_id_to_doc: dict[str, str] = {}
    pipe2 = client.pipeline()
    for doc_id, job_id in zip(document_ids, current_job_ids):
        if job_id:
            pipe2.get(_job_key(job_id))
            job_id_to_doc[job_id] = doc_id

    job_raws: list[str | None] = await pipe2.execute() if job_id_to_doc else []
    for job_id, raw in zip(job_id_to_doc.keys(), job_raws):
        doc_id = job_id_to_doc[job_id]
        job_states[doc_id] = json.loads(raw) if raw else None

    return {doc_id: job_states.get(doc_id) for doc_id in document_ids}
