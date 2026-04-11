#!/usr/bin/env python3
import json
import os
from pathlib import Path

import openai
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

from app.services.system_instructions import (
    get_instruction_text,
    load_instruction_set,
    render_instruction_template,
)
from app.services.legislation.submission import codes_match, get_submission_by_codes
from app.utils.legislation.get_legislation_by_section import (
    get_subsections_for_code,
    load_legislation,
)


METRICS_FILE = "app/utils/legislation/legislation_comparison_metrics.json"
OUTPUT_FILE = "app/utils/legislation/legislation_submission_issues.json"
SUBMISSION_FILE = "parsed_legislation_codes.json"


def init_openai_from_env():
    """
    Load OPENAI_API_KEY and OPENAI_MODEL from .env and configure openai.
    """
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in environment or .env")
    openai.api_key = api_key
    get_openai_model()


def get_openai_model():
    """
    Get the OpenAI model from environment.
    """
    model = os.getenv("OPENAI_MODEL")
    if not model:
        raise RuntimeError("OPENAI_MODEL not set in environment or .env")
    return model


def load_metrics():
    """
    Load legislation_comparison_metrics.json.
    Expected structure (at minimum):
      {
        "all_main_codes_found": [ "145.A.25", "145.A.30", ... ],
        ...
      }
    """
    path = Path(METRICS_FILE)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_submission_json():
    """
    Load parsed_legislation_codes.json for cross-referencing sections.
    """
    path = Path(SUBMISSION_FILE)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_sections_for_code(code, submission_data):
    """
    Given a legislation code, find all section numbers in parsed_legislation_codes.json
    where this code appears in the 'legislation_codes' list, using flexible matching.
    """
    sections = submission_data.get("sections", [])
    matching_sections = []
    for section in sections:
        leg_codes = section.get("legislation_codes", [])
        if any(codes_match(code, lc) for lc in leg_codes):
            matching_sections.append(section["section_number"])
    return matching_sections


class Issue(BaseModel):
    code: str
    submission_excerpt: str
    explanation: str
    legislation_source: str
    main_code: str | None = None
    submission_sections: list[str] = []
    severity: str = "info"


class IssueList(BaseModel):
    issues: list[Issue]


def call_openai_for_issues(code, legislation_markdown, submission_text):
    """
    Call OpenAI with structured output to get issues for a given code.
    Returns an IssueList instance (parsed Pydantic model).
    """
    instructions = load_instruction_set("legislation-compare.yaml")
    prompt = render_instruction_template(
        instructions,
        "compare.user_prompt",
        code=code,
        legislation_markdown=legislation_markdown,
        submission_text=submission_text,
    )
    client = OpenAI()

    completion = client.beta.chat.completions.parse(
        model=get_openai_model(),
        messages=[
            {
                "role": "system",
                "content": get_instruction_text(instructions, "compare.system_message"),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        response_format=IssueList,
    )

    return completion.choices[0].message.parsed


def main():
    init_openai_from_env()

    metrics = load_metrics()
    path = Path("app/utils/legislation/unique_sections_legislation.json")
    legislation = load_legislation(path)
    submission_data = load_submission_json()

    all_main_codes_found = metrics.get("all_main_codes_found", [])
    if not isinstance(all_main_codes_found, list):
        all_main_codes_found = []

    all_issues = []

    for code in all_main_codes_found:
        if not isinstance(code, str):
            continue

        leg_info = get_subsections_for_code(code, legislation)
        legislation_markdown = (leg_info.get("main_section") or "").strip()
        subsections_md = (leg_info.get("subsections_markdown") or "").strip()

        if subsections_md:
            if legislation_markdown:
                legislation_markdown = legislation_markdown + "\n\n\n" + subsections_md
            else:
                legislation_markdown = subsections_md

        submission_text = get_submission_by_codes([code])
        if not submission_text.strip():
            continue

        result: IssueList = call_openai_for_issues(code, legislation_markdown, submission_text)
        submission_sections = find_sections_for_code(code, submission_data)

        for issue in result.issues:
            issue.main_code = issue.main_code or code
            issue.submission_sections = submission_sections
            all_issues.append(issue.model_dump())

    output = {
        "issues": all_issues,
    }

    out_path = Path(OUTPUT_FILE)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
