import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.services.legislation.compare import IssueList, call_openai_for_issues, find_sections_for_code, init_openai_from_env
from app.services.legislation.submission import get_submission_by_codes
from app.utils.legislation.find_sections import compute_metrics, load_legislation_unique_sections, parse_submission_codes
from app.utils.legislation.get_legislation_by_section import get_subsections_for_code, load_legislation
from app.services.legislation.parser import LegislationCodeParser
from app.services.pdf.converter import convert_pdf_to_markdown

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Legislation"])

MOCK_RESPONSE_FILE = Path("full_response.json")
mock_response = None
if MOCK_RESPONSE_FILE.exists():
    try:
        with open(MOCK_RESPONSE_FILE, "r", encoding="utf-8") as f:
            mock_response = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load mock response from %s: %s", MOCK_RESPONSE_FILE, exc)

jobs: dict[str, dict] = {}


class ParseResponse(BaseModel):
    markdown: str
    parsed_codes: dict
    metrics: dict
    issues: dict


class JobResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: ParseResponse | None = None


def process_legislation(job_id: str, file_content: bytes):
    jobs[job_id]["status"] = "processing"

    temp_pdf_path = None
    temp_md_path = None
    temp_parsed_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
            temp_pdf_path = temp_pdf.name
            temp_pdf.write(file_content)

        temp_md_path = temp_pdf_path.replace(".pdf", ".md")
        convert_pdf_to_markdown(temp_pdf_path, temp_md_path)

        with open(temp_md_path, "r", encoding="utf-8") as f:
            full_markdown = f.read()

        parser = LegislationCodeParser()
        parsed_codes = parser.parse_markdown(temp_md_path)

        legislation_path = Path("app/utils/legislation/unique_sections_legislation.json")
        legislation_info = load_legislation_unique_sections(legislation_path)

        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as temp_parsed:
            json.dump(parsed_codes, temp_parsed)
            temp_parsed_path = temp_parsed.name

        submission_raw_codes, submission_norm_codes = parse_submission_codes(Path(temp_parsed_path))
        metrics = compute_metrics(legislation_info, submission_raw_codes, submission_norm_codes)

        init_openai_from_env()
        legislation = load_legislation(Path("app/utils/legislation/legislation.json"))

        all_main_codes_found = metrics.get("all_main_codes_found", [])
        all_issues = []

        for code in all_main_codes_found:
            if not isinstance(code, str):
                continue

            leg_info = get_subsections_for_code(code, legislation)
            legislation_markdown = (leg_info.get("main_section") or "").strip()
            subsections_md = (leg_info.get("subsections_markdown") or "").strip()
            if subsections_md:
                if legislation_markdown:
                    legislation_markdown += "\n\n\n" + subsections_md
                else:
                    legislation_markdown = subsections_md

            submission_text = get_submission_by_codes([code], parsed_codes)
            if not submission_text.strip():
                continue

            result: IssueList = call_openai_for_issues(code, legislation_markdown, submission_text)
            submission_sections = find_sections_for_code(code, parsed_codes)

            for issue in result.issues:
                issue.main_code = issue.main_code or code
                issue.submission_sections = submission_sections
                all_issues.append(issue.model_dump())

        issues_output = {"issues": all_issues}

        result_data = ParseResponse(
            markdown=full_markdown,
            parsed_codes=parsed_codes,
            metrics=metrics,
            issues=issues_output,
        )
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["result"] = result_data

    except Exception as exc:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(exc)

    finally:
        for path in [temp_pdf_path, temp_md_path, temp_parsed_path]:
            if path and os.path.exists(path):
                os.unlink(path)


@router.post("/api/parse-legislation", response_model=JobResponse)
async def parse_legislation(file: UploadFile = File(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")

    file_content = await file.read()
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "result": None, "error": None}
    background_tasks.add_task(process_legislation, job_id, file_content)
    return JobResponse(job_id=job_id, status="pending")


@router.get("/api/parse-legislation-status/{job_id}", response_model=JobStatusResponse)
async def parse_legislation_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    if job["status"] == "completed":
        return JobStatusResponse(job_id=job_id, status="completed", result=job["result"])
    if job["status"] == "failed":
        raise HTTPException(status_code=500, detail=f"Job failed: {job['error']}")
    return JobStatusResponse(job_id=job_id, status=job["status"])


@router.get("/api/parse-legislation-mock", response_model=ParseResponse)
async def parse_legislation_mock():
    if mock_response:
        return ParseResponse(**mock_response)
    raise HTTPException(status_code=404, detail="Mock response not available")
