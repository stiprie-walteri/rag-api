import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 3600       # 1 hour  — max time an evaluation can run
JOB_TTL_SECONDS = 86400       # 24 hours — how long results stay in Redis
CURRENT_TTL_SECONDS = 90000   # slightly longer than job so pointer outlives the state


_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    global _client
    if _client is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _client = redis.from_url(url, decode_responses=True)
    return _client


# ── Key helpers ──────────────────────────────────────────────────────────────

def _lock_key(document_id: str) -> str:
    return f"eval:lock:{document_id}"


def _job_key(job_id: str) -> str:
    return f"eval:job:{job_id}"


def _current_key(document_id: str) -> str:
    return f"eval:current:{document_id}"


# ── Lock management ───────────────────────────────────────────────────────────

async def acquire_lock(document_id: str, job_id: str) -> bool:
    """SET NX — returns True only if no evaluation was already running."""
    client = get_redis_client()
    result = await client.set(_lock_key(document_id), job_id, nx=True, ex=LOCK_TTL_SECONDS)
    return result is not None


async def release_lock(document_id: str) -> None:
    client = get_redis_client()
    await client.delete(_lock_key(document_id))


async def is_locked(document_id: str) -> bool:
    client = get_redis_client()
    return await client.exists(_lock_key(document_id)) == 1


# ── Job state ─────────────────────────────────────────────────────────────────

async def start_job(
    *,
    job_id: str,
    document_id: str,
    organization_id: str,
    version_id: str,
    total_tasks: int,
) -> None:
    client = get_redis_client()
    now = datetime.now(timezone.utc).isoformat()
    state: dict[str, Any] = {
        "job_id": job_id,
        "status": "running",
        "organization_id": organization_id,
        "document_id": document_id,
        "version_id": version_id,
        "total_tasks": total_tasks,
        "completed_count": 0,
        "current_task": None,
        "results": [],
        "error": None,
        "started_at": now,
        "updated_at": now,
    }
    pipe = client.pipeline()
    pipe.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)
    pipe.set(_current_key(document_id), job_id, ex=CURRENT_TTL_SECONDS)
    await pipe.execute()


async def update_progress(job_id: str, *, current_task: list[str], completed_count: int) -> None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    if not raw:
        return
    state = json.loads(raw)
    state["current_task"] = current_task
    state["completed_count"] = completed_count
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    await client.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


async def append_result(job_id: str, *, result: dict[str, Any], completed_count: int) -> None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    if not raw:
        return
    state = json.loads(raw)
    state["results"].append(result)
    state["completed_count"] = completed_count
    state["current_task"] = None
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    await client.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


async def complete_job(job_id: str) -> None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    if not raw:
        return
    state = json.loads(raw)
    state["status"] = "completed"
    state["current_task"] = None
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    await client.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


async def fail_job(job_id: str, error: str) -> None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    if not raw:
        return
    state = json.loads(raw)
    state["status"] = "failed"
    state["error"] = error
    state["current_task"] = None
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    await client.set(_job_key(job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


# ── Reads ─────────────────────────────────────────────────────────────────────

async def get_job_state(job_id: str) -> dict[str, Any] | None:
    client = get_redis_client()
    raw = await client.get(_job_key(job_id))
    return json.loads(raw) if raw else None


async def get_current_job_id(document_id: str) -> str | None:
    client = get_redis_client()
    return await client.get(_current_key(document_id))


async def get_job_states_for_documents(document_ids: list[str]) -> dict[str, dict[str, Any] | None]:
    """Batch-fetch job states for a list of document IDs.

    Returns a mapping of document_id -> job state dict (or None if no active job).
    """
    if not document_ids:
        return {}

    client = get_redis_client()

    # Fetch all current job_id pointers in one pipeline
    pipe = client.pipeline()
    for doc_id in document_ids:
        pipe.get(_current_key(doc_id))
    current_job_ids: list[str | None] = await pipe.execute()

    # Fetch job states for those that have a pointer
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
