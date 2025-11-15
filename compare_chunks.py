#!/usr/bin/env python3
import json
import os
from pathlib import Path
import openai
from openai import OpenAI
from pydantic import BaseModel
from dotenv import load_dotenv

from legislation_util.get_legislation_by_section import load_legislation, get_subsections_for_code
from get_submission_chunks import get_submission_by_codes


# Hard-coded inputs
METRICS_FILE = "legislation_util/legislation_comparison_metrics.json"
OUTPUT_FILE = "legislation_util/legislation_submission_issues.json"

# Prompt template sent to OpenAI
PROMPT_TEMPLATE = """
You are an expert in EASA Part-145 regulatory compliance and aviation maintenance oversight.

Your task:
Given:
- A specific legislation section code: "{code}"
- The full legislation text for that section (including related AMC/GM) in Markdown
- The company's submission text relevant to that code

Identify concrete places where the submission does NOT fully comply with the legislation, or where it is ambiguous/risky relative to the legislation.

Return a SINGLE JSON object with the following structure:

{{
  "issues": [
    {{
      "code": "string, the most specific legislation section or subsection (e.g. '145.A.25(c)(1)' or 'AMC1 145.A.25 (a)')",
      "submission_excerpt": "string, short verbatim excerpt from the submission that is problematic",
      "explanation": "string, short and clear explanation (2–4 sentences) of what is wrong or missing and why",
      "legislation_source": "string, either the exact legislation quote or a concise paraphrase with the precise source id"
    }}
  ]
}}

Rules:
- If there are NO problems or gaps, return: {{ "issues": [] }}
- Do NOT invent issues: only flag something if it is clearly weaker, conflicting, incomplete, or ambiguous vs the legislation.
- Prefer a few strong, well-explained issues over many minor ones.
- "submission_excerpt" MUST be exact text copied from the submission (no paraphrase).
- "legislation_source" MUST clearly identify the relevant part of the legislation (id plus text or a precise paraphrase).

Legislation (Markdown):
---
{legislation_markdown}
---

Submission:
---
{submission_text}
---
"""


def init_openai_from_env():
    """
    Load OPENAI_API_KEY from .env and configure openai.
    """
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in environment or .env")
    openai.api_key = api_key


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

def init_openai_from_env():
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in environment or .env")
    openai.api_key = api_key

class Issue(BaseModel):
    code: str
    submission_excerpt: str
    explanation: str
    legislation_source: str
    # optional extra tag you were adding
    main_code: str | None = None


class IssueList(BaseModel):
    issues: list[Issue]

def call_openai_for_issues(code, legislation_markdown, submission_text):
    """
    Call OpenAI with structured output to get issues for a given code.
    Returns an IssueList instance (parsed Pydantic model).
    """
    prompt = PROMPT_TEMPLATE.format(
        code=code,
        legislation_markdown=legislation_markdown,
        submission_text=submission_text,
    )
    client = OpenAI()
    response = client.responses.parse(
        model="gpt-5-mini",
        input=[
            {
                "role": "system",
                "content": (
                    "You are a strict, detail-oriented compliance analyst "
                    "for aviation maintenance regulation (EASA Part-145). "
                    "Always respond with valid JSON matching the given schema."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        text_format=IssueList
    )

    return response.output_parsed


def main():
    init_openai_from_env()

    # Load metrics and legislation
    metrics = load_metrics()
    path = Path("legislation_util/unique_sections_legislation.json")
    legislation = load_legislation(path)

    all_main_codes_found = metrics.get("all_main_codes_found", [])
    if not isinstance(all_main_codes_found, list):
        all_main_codes_found = []

    all_issues = []

    for code in all_main_codes_found:
        if not isinstance(code, str):
            continue

        # Get legislation description and subsections for this code
        leg_info = get_subsections_for_code(code, legislation)
        legislation_markdown = (leg_info.get("main_section") or "").strip()
        subsections_md = (leg_info.get("subsections_markdown") or "").strip()

        if subsections_md:
            if legislation_markdown:
                legislation_markdown = legislation_markdown + "\n\n\n" + subsections_md
            else:
                legislation_markdown = subsections_md

        # Get submission text relevant to this code (TODO implementation)
        submission_text = get_submission_by_codes([code])

        # Skip if there is no submission text at all
        if not submission_text.strip():
            continue

        result: IssueList = call_openai_for_issues(code, legislation_markdown, submission_text)

        for issue in result.issues:
            issue.main_code = issue.main_code or code
            all_issues.append(issue.model_dump())

    output = {
        "issues": all_issues,
    }

    out_path = Path(OUTPUT_FILE)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
