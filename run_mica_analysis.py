import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from openai import APITimeoutError, RateLimitError


if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def load_env() -> None:
    vals = dotenv_values(".env")
    for key, value in vals.items():
        if value is not None:
            os.environ.setdefault(key, value)

    postgres_user = os.environ.get("POSTGRES_USER", "rag_user")
    postgres_password = os.environ.get("POSTGRES_PASSWORD", "rag_password")
    postgres_host = os.environ.get("POSTGRES_HOST", "localhost")
    postgres_port = os.environ.get("POSTGRES_PORT", "5432")
    postgres_db = os.environ.get("POSTGRES_DB", "rag_api")
    os.environ["POSTGRES_DSN"] = os.environ.get(
        "POSTGRES_DSN",
        f"postgresql://{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}/{postgres_db}",
    )
    os.environ["MINIO_ENDPOINT"] = os.environ.get(
        "MINIO_ENDPOINT",
        f"localhost:{os.environ.get('MINIO_API_PORT', '9000')}",
    )
    os.environ["REDIS_URL"] = os.environ.get("REDIS_URL", "redis://localhost:6379")
    os.environ["MINIO_SECURE"] = os.environ.get("MINIO_SECURE", "false")
    os.environ["LEGISLATION_TEMPLATES_DIR"] = os.environ.get(
        "LEGISLATION_TEMPLATES_DIR",
        "legislation-templates",
    )
    os.environ["AGENT_CONCURRENCY"] = "1"


def resolve_pandoc() -> str:
    candidates = [
        shutil.which("pandoc"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Pandoc" / "pandoc.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError("pandoc was not found")


def convert_to_markdown(input_path: Path) -> Path:
    if input_path.suffix.lower() in {".md", ".markdown"}:
        return input_path

    if input_path.suffix.lower() != ".docx":
        raise ValueError(f"Unsupported input format: {input_path.suffix}")

    pandoc = resolve_pandoc()
    output_path = input_path.with_suffix(".md")
    subprocess.run(
        [pandoc, str(input_path), "-t", "gfm", "-o", str(output_path)],
        check=True,
    )
    return output_path


def _clean_heading(text: str) -> str:
    cleaned = re.sub(r"[*_`]", "", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def build_markdown_chunks(markdown_text: str) -> list[dict]:
    lines = markdown_text.splitlines()
    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")
    chunks: list[dict] = []
    current_heading: tuple[int, str] | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current_heading, buffer
        if current_heading is None and not buffer:
            return
        level, title = current_heading or (1, "Preamble")
        text = "\n".join(buffer).strip()
        if not text:
            buffer = []
            return
        chunk_no = len(chunks) + 1
        chunks.append(
            {
                "level": level,
                "title": title,
                "start_page": chunk_no,
                "end_page": chunk_no,
                "text": text,
            }
        )
        buffer = []

    for line in lines:
        match = heading_re.match(line)
        if match:
            flush()
            current_heading = (len(match.group(1)), _clean_heading(match.group(2)))
            buffer = [line]
            continue
        buffer.append(line)

    flush()
    return chunks


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="Nexus_Investment_Program_of_Operations_MiCA.docx",
        help="Path to the source document (.docx or .md)",
    )
    parser.add_argument(
        "--template",
        default="legislation-templates/mica_programme_of_operations_agent_instructions.yaml",
        help="Path to the YAML template",
    )
    parser.add_argument(
        "--output",
        default="nexus_mica_analysis_results.json",
        help="Path to the full output JSON artifact",
    )
    parser.add_argument(
        "--summary",
        default="nexus_mica_analysis_summary.json",
        help="Path to the summary JSON artifact",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=8,
        help="Maximum retries for a single task on rate limit",
    )
    return parser.parse_args()


def main() -> int:
    load_env()

    from app.api.dependencies import require_docstore
    from app.core.auth import AuthContext, PUBLIC_PATHS, get_auth_context
    from app.main import app
    from app.services.evaluation.agent import evaluate_task_with_agent

    args = parse_args()
    input_path = Path(args.input).resolve()
    template_path = Path(args.template).resolve()
    output_path = Path(args.output).resolve()
    summary_path = Path(args.summary).resolve()

    markdown_path = convert_to_markdown(input_path)
    markdown_text = markdown_path.read_text(encoding="utf-8")
    chunks_to_save = build_markdown_chunks(markdown_text)

    with template_path.open("r", encoding="utf-8") as f:
        template = yaml.safe_load(f)

    if "/api/" not in PUBLIC_PATHS:
        PUBLIC_PATHS.append("/api/")

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id="codex-test-user",
        email="codex@example.com",
        first_name="Codex",
        last_name="Test",
    )

    bundle: dict = {
        "source_document": str(input_path),
        "converted_markdown": str(markdown_path),
        "template_id": template.get("id"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": [],
        "failures": [],
    }

    with TestClient(app) as client:
        me_resp = client.get("/api/me")
        me_resp.raise_for_status()
        me = me_resp.json()
        org_id = me["organization_id"]
        bundle["me"] = me

        project_resp = client.post(
            f"/api/orgs/{org_id}/projects",
            json={
                "name": "Nexus MiCA Analysis",
                "description": "Converted Nexus investment document analysis",
            },
        )
        project_resp.raise_for_status()
        project = project_resp.json()
        project_id = project["project_id"]
        bundle["project"] = project

        with markdown_path.open("rb") as f:
            upload_resp = client.post(
                "/api/documents/upload",
                data={
                    "organization_id": org_id,
                    "project_id": project_id,
                    "title": input_path.stem,
                    "message": "Converted from DOCX and uploaded for MiCA analysis",
                },
                files={"file": (markdown_path.name, f, "text/markdown")},
            )
        upload_resp.raise_for_status()
        upload = upload_resp.json()
        bundle["upload"] = upload

        service = require_docstore()
        asyncio.run(
            service.save_document_chunks(
                organization_id=org_id,
                document_id=upload["document_id"],
                version_id=upload["version_id"],
                chunks=chunks_to_save,
            )
        )

        chunks_resp = client.get(
            f"/api/orgs/{org_id}/documents/{upload['document_id']}/versions/{upload['version_no']}/chunks"
        )
        chunks_resp.raise_for_status()
        stored_chunks = chunks_resp.json()["chunks"]
        bundle["chunks"] = {
            "count": len(stored_chunks),
            "sample_titles": [c.get("title") for c in stored_chunks[:20]],
        }

        tasks = template["Tasks"]
        system_prompt = template.get("system_prompt")
        references = template.get("references")

        for idx, task in enumerate(tasks, start=1):
            last_error = None
            for attempt in range(1, args.max_retries + 1):
                try:
                    print(f"Running task {idx}/{len(tasks)} attempt {attempt}: {task[0]}", flush=True)
                    result = asyncio.run(
                        evaluate_task_with_agent(
                            task_list=task,
                            chunks=stored_chunks,
                            system_prompt_override=system_prompt,
                            references=references,
                        )
                    )
                    payload = result.model_dump()
                    payload["task_index"] = idx
                    payload["attempts"] = attempt
                    bundle["results"].append(payload)
                    write_json(output_path, bundle)
                    time.sleep(5)
                    break
                except (RateLimitError, APITimeoutError) as exc:
                    last_error = str(exc)
                    wait_seconds = min(180, 20 * attempt)
                    reason = "Rate limited" if isinstance(exc, RateLimitError) else "Timed out"
                    print(
                        f"{reason} on task {idx}. Sleeping {wait_seconds}s before retry.",
                        flush=True,
                    )
                    time.sleep(wait_seconds)
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    print(f"Task {idx} failed: {last_error}", flush=True)
                    break

            if len(bundle["results"]) < idx:
                bundle["failures"].append(
                    {
                        "task_index": idx,
                        "task": task,
                        "error": last_error,
                    }
                )
                write_json(output_path, bundle)
                break

        if not bundle["failures"] and len(bundle["results"]) == len(tasks):
            compliance_result = {
                "template_id": template.get("id"),
                "results": bundle["results"],
            }
            asyncio.run(
                service.save_compliance_result(
                    organization_id=org_id,
                    version_id=upload["version_id"],
                    result=compliance_result,
                )
            )
            bundle["compliance_saved"] = True
        else:
            bundle["compliance_saved"] = False

    bundle["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(output_path, bundle)

    summary = {
        "source_document": str(input_path),
        "converted_markdown": str(markdown_path),
        "organization_id": bundle["me"]["organization_id"],
        "document_id": bundle["upload"]["document_id"],
        "version_id": bundle["upload"]["version_id"],
        "version_no": bundle["upload"]["version_no"],
        "chunk_count": bundle["chunks"]["count"],
        "total_tasks": len(template["Tasks"]),
        "completed_tasks": len(bundle["results"]),
        "failed_tasks": len(bundle["failures"]),
        "all_completed": len(bundle["results"]) == len(template["Tasks"]) and not bundle["failures"],
        "output_path": str(output_path),
        "finished_at": bundle["finished_at"],
    }
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
