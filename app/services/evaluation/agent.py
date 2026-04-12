import json
import logging
import hashlib
import os
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from json_repair import repair_json
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.services.system_instructions import (
    get_instruction_text,
    load_instruction_set,
    render_instruction_template,
)

load_dotenv()
logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")
AGENT_MAX_TOOL_CALLS = int(os.getenv("AGENT_MAX_TOOL_CALLS", "25"))
AGENT_REQUEST_TIMEOUT = int(os.getenv("AGENT_REQUEST_TIMEOUT", "120"))
AGENT_SUMMARY_BATCH_CHARS = int(os.getenv("AGENT_SUMMARY_BATCH_CHARS", "18000"))
AGENT_SUMMARY_SECTION_CHARS = int(os.getenv("AGENT_SUMMARY_SECTION_CHARS", "1800"))

_client: Optional[AsyncOpenAI] = None


def get_openrouter_client() -> AsyncOpenAI:
    global _client
    if not _client:
        if not OPENROUTER_API_KEY:
            raise ValueError("OPENROUTER_API_KEY is not set.")
        _client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )
    return _client


def _load_evaluation_agent_instructions() -> dict[str, Any]:
    return load_instruction_set("evaluation-agent.yaml")


class ReasoningStep(BaseModel):
    step: int
    thought: Optional[str] = None          # model's reasoning text before the tool call
    sections_queried: List[int] = Field(default_factory=list)        # section indexes it decided to fetch
    section_titles: List[str] = Field(default_factory=list)          # human-readable titles for those indexes
    references_queried: List[str] = Field(default_factory=list)     # legislation reference IDs fetched (e.g. R1, R3)


class IssueSectionReference(BaseModel):
    id: Optional[str] = None
    title: Optional[str] = None
    quote: Optional[str] = None


class SuggestedInsertLocation(BaseModel):
    action: str = "insert"
    target_section_id: Optional[str] = None
    target_section_title: Optional[str] = None
    anchor_quote: Optional[str] = None
    placement: str = "after"
    start_index: Optional[int] = None
    end_index: Optional[int] = None


class SuggestedFix(BaseModel):
    insertable_text: str = ""
    insert_location: SuggestedInsertLocation = Field(default_factory=SuggestedInsertLocation)


class DocumentationIssue(BaseModel):
    issue_id: Optional[str] = None
    issue_type: str = "documentation_issue"
    title: str = ""
    legislation_reference: Optional[str] = None
    current_section: Optional[IssueSectionReference] = None
    problem: str = ""
    solution: str = ""
    suggested_fix: SuggestedFix = Field(default_factory=SuggestedFix)


class TaskEvaluationResult(BaseModel):
    legislation_id: Optional[str] = None
    legislation_name: Optional[str] = None
    task: List[str]
    status: str = "completed"
    exists: bool
    explanation: str
    missing_sections: List[str] = Field(default_factory=list)
    incorrect_sections: List[Dict[str, str]] = Field(default_factory=list)
    issues: List[DocumentationIssue] = Field(default_factory=list)
    reasoning_steps: List[ReasoningStep] = Field(default_factory=list)


def _format_toc(chunks: List[Dict[str, Any]]) -> str:
    toc_lines = []
    current_doc = None
    for i, chunk in enumerate(chunks):
        title = chunk.get("title") or "Unnamed Section"
        doc_part = title.split(" :: ")[0] if " :: " in title else None
        if doc_part and doc_part != current_doc:
            if current_doc is not None:
                toc_lines.append("")
            toc_lines.append(f"--- {doc_part} ---")
            current_doc = doc_part
        level_prefix = "  " * (chunk.get("chunk_level", 1) - 1)
        toc_lines.append(
            f"{level_prefix}{i}: {title} "
            f"(Pages {chunk.get('start_page', '?')}-{chunk.get('end_page', '?')})"
        )
    return "\n".join(toc_lines)

def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _first_text(mapping: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in mapping:
            text = _clean_text(mapping.get(key))
            if text:
                return text
    return ""


def _first_value(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _parse_current_section(raw_issue: Dict[str, Any]) -> Optional[IssueSectionReference]:
    section_raw = _first_value(
        raw_issue,
        "Current Section",
        "current_section",
        "CurrentSection",
        "section",
    )
    if isinstance(section_raw, dict):
        section = IssueSectionReference(
            id=_first_text(section_raw, "ID", "id", "Section ID", "section_id"),
            title=_first_text(section_raw, "Title", "title", "Section Title", "section_title"),
            quote=_first_text(section_raw, "Quote", "quote", "Problem Quote", "problem_quote"),
        )
    else:
        section = IssueSectionReference(
            id=_first_text(raw_issue, "ID", "id", "Section ID", "section_id"),
            title=_first_text(raw_issue, "Section Title", "section_title"),
            quote=_first_text(raw_issue, "Quote", "quote", "Problem Quote", "problem_quote"),
        )

    if section.id or section.title or section.quote:
        return section
    return None


def _parse_issue(raw_issue: Any) -> Optional[DocumentationIssue]:
    if not isinstance(raw_issue, dict):
        return None

    fix_raw = _first_value(
        raw_issue,
        "Suggested Fix",
        "suggested_fix",
        "Fix",
        "fix",
        "Suggested Insertion",
        "suggested_insertion",
    )
    if not isinstance(fix_raw, dict):
        fix_raw = {}

    location_raw = _first_value(
        fix_raw,
        "Insert Location",
        "insert_location",
        "Location",
        "location",
    )
    if not isinstance(location_raw, dict):
        location_raw = {}

    issue_type = _first_text(raw_issue, "Type", "type", "issue_type") or "documentation_issue"
    title = _first_text(raw_issue, "Title", "title", "Issue", "issue")
    problem = _first_text(raw_issue, "Problem", "problem", "Comment", "comment")
    solution = _first_text(raw_issue, "Solution", "solution", "Resolution", "resolution")
    legislation_reference = _first_text(
        raw_issue,
        "Legislation Reference",
        "legislation_reference",
        "Reference",
        "reference",
    )

    insertable_text = (
        _first_text(
            fix_raw,
            "Insertable Text",
            "insertable_text",
            "Example Text",
            "example_text",
            "Suggested Text",
            "suggested_text",
        )
        or _first_text(
            raw_issue,
            "Insertable Text",
            "insertable_text",
            "Example Text",
            "example_text",
            "Suggested Text",
            "suggested_text",
        )
    )

    insert_location = SuggestedInsertLocation(
        action=(
            _first_text(location_raw, "Action", "action")
            or _first_text(fix_raw, "Action", "action")
            or "insert"
        ),
        target_section_id=(
            _first_text(location_raw, "Target Section ID", "target_section_id", "ID", "id")
            or _first_text(fix_raw, "Target Section ID", "target_section_id")
            or None
        ),
        target_section_title=(
            _first_text(location_raw, "Target Section Title", "target_section_title", "Title", "title")
            or _first_text(fix_raw, "Target Section Title", "target_section_title")
            or None
        ),
        anchor_quote=(
            _first_text(location_raw, "Anchor Quote", "anchor_quote", "Anchor Text", "anchor_text")
            or _first_text(fix_raw, "Anchor Quote", "anchor_quote", "Anchor Text", "anchor_text")
            or None
        ),
        placement=(
            _first_text(location_raw, "Placement", "placement")
            or _first_text(fix_raw, "Placement", "placement")
            or "after"
        ),
    )

    issue = DocumentationIssue(
        issue_id=_first_text(raw_issue, "Issue ID", "issue_id", "id") or None,
        issue_type=issue_type,
        title=title,
        legislation_reference=legislation_reference or None,
        current_section=_parse_current_section(raw_issue),
        problem=problem,
        solution=solution,
        suggested_fix=SuggestedFix(
            insertable_text=insertable_text,
            insert_location=insert_location,
        ),
    )

    if (
        issue.title
        or issue.problem
        or issue.solution
        or issue.suggested_fix.insertable_text
    ):
        return issue
    return None


def _assign_issue_id(issue: DocumentationIssue, index: int) -> DocumentationIssue:
    if issue.issue_id:
        return issue
    fingerprint = "|".join(
        [
            issue.issue_type,
            issue.title,
            issue.legislation_reference or "",
            issue.problem,
            issue.suggested_fix.insertable_text[:200],
        ]
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:12]
    issue.issue_id = f"issue-{index + 1}-{digest}"
    return issue


def _extract_json_object(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1]
    if "```" in cleaned:
        cleaned = cleaned.split("```")[0]
    cleaned = cleaned.strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end >= start:
        cleaned = cleaned[start : end + 1]

    repaired = repair_json(cleaned)
    data = json.loads(repaired)
    if not isinstance(data, dict):
        raise ValueError("Evaluation response was not a JSON object.")
    return data


def _parse_evaluation_output(
    final_text: str,
) -> tuple[bool, str, List[str], List[Dict[str, str]], List[DocumentationIssue]]:
    data = _extract_json_object(final_text)

    exists = bool(data.get("exists", False))
    explanation = _clean_text(data.get("explanation")) or final_text

    missing_sections: List[str] = []
    ms_val = data.get("Missing Sections", data.get("missing_sections", []))
    if isinstance(ms_val, list):
        missing_sections = [_clean_text(x) for x in ms_val if _clean_text(x)]

    incorrect_sections: List[Dict[str, str]] = []
    is_val = data.get("Incorrect Sections", data.get("incorrect_sections", []))
    if isinstance(is_val, list):
        cleaned_sections: List[Dict[str, str]] = []
        for item in is_val:
            if not isinstance(item, dict):
                continue
            cleaned_sections.append({
                "ID": _first_text(item, "ID", "id"),
                "Quote": _first_text(item, "Quote", "quote"),
                "Comment": _first_text(item, "Comment", "comment"),
            })
        incorrect_sections = [
            x for x in cleaned_sections if x.get("ID") or x.get("Quote") or x.get("Comment")
        ]

    issues: List[DocumentationIssue] = []
    issues_val = (
        data.get("Issues")
        or data.get("issues")
        or data.get("Documentation Issues")
        or data.get("documentation_issues")
        or data.get("Issue Suggestions")
        or data.get("issue_suggestions")
        or []
    )
    if isinstance(issues_val, list):
        for raw_issue in issues_val:
            issue = _parse_issue(raw_issue)
            if issue is not None:
                issues.append(_assign_issue_id(issue, len(issues)))

    return exists, explanation, missing_sections, incorrect_sections, issues


def _issue_suggestions_complete(
    exists: bool,
    missing_sections: List[str],
    incorrect_sections: List[Dict[str, str]],
    issues: List[DocumentationIssue],
) -> bool:
    issue_count = len(missing_sections) + len(incorrect_sections)
    if issue_count == 0:
        return exists and not issues
    if len(issues) < issue_count:
        return False

    for issue in issues:
        location = issue.suggested_fix.insert_location
        has_location = bool(
            location.target_section_id
            or location.target_section_title
            or location.anchor_quote
        )
        if not issue.solution.strip():
            return False
        if not issue.suggested_fix.insertable_text.strip():
            return False
        if not has_location:
            return False
    return True


def _build_document_manifest(
    chunks: List[Dict[str, Any]],
    *,
    instructions: dict[str, Any],
) -> str:
    doc_ranges: Dict[str, List[int]] = {}
    for i, chunk in enumerate(chunks):
        title = chunk.get("title") or ""
        doc_title = title.split(" :: ")[0].strip() if " :: " in title else ""
        if not doc_title:
            doc_title = chunk.get("document_id", "Unknown Document")
        doc_ranges.setdefault(doc_title, []).append(i)

    if len(doc_ranges) <= 1:
        return ""

    items = [
        render_instruction_template(
            instructions,
            "document_manifest.item",
            position=idx,
            doc_title=doc_title,
            start_index=min(indexes),
            end_index=max(indexes),
        ).rstrip()
        for idx, (doc_title, indexes) in enumerate(doc_ranges.items(), 1)
    ]

    return render_instruction_template(
        instructions,
        "document_manifest.block",
        document_count=len(doc_ranges),
        items="\n".join(items),
    )


def _compact_text(text: str, *, max_chars: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized

    clipped = normalized[:max_chars].rstrip()
    last_space = clipped.rfind(" ")
    if last_space > max_chars // 2:
        clipped = clipped[:last_space]
    return f"{clipped}..."


def _build_summary_batches(chunks: List[Dict[str, Any]]) -> List[str]:
    batches: List[str] = []
    current_batch: List[str] = []
    current_size = 0

    for idx, chunk in enumerate(chunks):
        title = chunk.get("title") or "Unnamed Section"
        text_content = _compact_text(
            str(chunk.get("text_content", "")),
            max_chars=AGENT_SUMMARY_SECTION_CHARS,
        )
        batch_part = (
            f"Section {idx}\n"
            f"Title: {title}\n"
            f"Pages: {chunk.get('start_page', '?')}-{chunk.get('end_page', '?')}\n"
            f"Excerpt:\n{text_content}"
        )

        if current_batch and current_size + len(batch_part) > AGENT_SUMMARY_BATCH_CHARS:
            batches.append("\n\n".join(current_batch))
            current_batch = []
            current_size = 0

        current_batch.append(batch_part)
        current_size += len(batch_part)

    if current_batch:
        batches.append("\n\n".join(current_batch))

    return batches


async def _generate_plaintext_completion(
    *,
    system_prompt: str,
    user_prompt: str,
) -> str:
    client = get_openrouter_client()
    response = await client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        timeout=AGENT_REQUEST_TIMEOUT,
    )
    return (response.choices[0].message.content or "").strip()


async def build_exploratory_document_summary(
    chunks: List[Dict[str, Any]],
    *,
    documents: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    if not chunks:
        return None

    try:
        instructions = _load_evaluation_agent_instructions()
        chunk_batches = _build_summary_batches(chunks)
        if not chunk_batches:
            return None

        document_titles = [
            str(item.get("title")).strip()
            for item in (documents or [])
            if str(item.get("title") or "").strip()
        ]
        if document_titles:
            document_context = "Documents in corpus: " + ", ".join(document_titles[:20])
        else:
            document_context = f"Sections in corpus: {len(chunks)}"

        batch_system_prompt = get_instruction_text(
            instructions,
            "exploratory_summary.batch_system_prompt",
        )

        batch_summaries: List[str] = []
        for batch_index, batch_text in enumerate(chunk_batches, start=1):
            batch_summary = await _generate_plaintext_completion(
                system_prompt=batch_system_prompt,
                user_prompt=render_instruction_template(
                    instructions,
                    "exploratory_summary.batch_user_prompt",
                    document_context=document_context,
                    batch_index=batch_index,
                    batch_count=len(chunk_batches),
                    batch_text=batch_text,
                ),
            )
            if batch_summary:
                batch_summaries.append(batch_summary)

        if not batch_summaries:
            return None

        if len(batch_summaries) == 1:
            return batch_summaries[0]

        final_summary = await _generate_plaintext_completion(
            system_prompt=get_instruction_text(
                instructions,
                "exploratory_summary.final_system_prompt",
            ),
            user_prompt=render_instruction_template(
                instructions,
                "exploratory_summary.final_user_prompt",
                document_context=document_context,
                batch_count=len(batch_summaries),
                batch_summaries="\n\n".join(
                    f"Batch summary {idx}:\n{summary}"
                    for idx, summary in enumerate(batch_summaries, start=1)
                ),
            ),
        )
        return final_summary or "\n\n".join(batch_summaries)
    except Exception as exc:
        logger.warning("[eval] Failed to build exploratory summary: %s", exc)
        return None


async def evaluate_task_with_agent(
    task_list: List[str],
    chunks: List[Dict[str, Any]],
    system_prompt_override: Optional[str] = None,
    references: Optional[Dict[str, Dict[str, str]]] = None,
    exploratory_summary: Optional[str] = None,
) -> TaskEvaluationResult:
    try:
        client = get_openrouter_client()
    except ValueError as exc:
        return TaskEvaluationResult(
            task=task_list,
            exists=False,
            explanation=f"Agent setup failed: {str(exc)}",
        )

    instructions = _load_evaluation_agent_instructions()
    task_flattened = "\n".join(f"- {task}" for task in task_list)
    toc_str = _format_toc(chunks)
    document_manifest = _build_document_manifest(chunks, instructions=instructions)
    refs = references or {}

    exploratory_summary_block = ""
    if exploratory_summary:
        exploratory_summary_block = render_instruction_template(
            instructions,
            "task_evaluation.exploratory_summary_block",
            exploratory_summary=exploratory_summary,
        )

    task_label = task_list[0][:60] + ("..." if len(task_list[0]) > 60 else "")
    logger.info("[eval] Starting - task: %s | chunks available: %d", task_label, len(chunks))

    output_format_block = """
==============================================================================
OUTPUT FORMAT -- MANDATORY
==============================================================================
Do not use emojis anywhere in your response.
When you have finished gathering information, output a raw JSON object as your
final response. DO NOT wrap it in markdown (no ```json fences).

Your final JSON MUST match this structure exactly:
{
  "exists": true | false,
  "explanation": "string",
  "Missing Sections": ["string", "string"],
  "Incorrect Sections": [
    {
      "ID": "string (section/chunk id or article reference)",
      "Quote": "string (direct quote from the document)",
      "Comment": "string (what is wrong or insufficient, and which topics/sections are required)"
    }
  ],
  "Issues": [
    {
      "Issue ID": "stable unique issue id, or omit and the backend will assign one",
      "Type": "missing_section | incorrect_section",
      "Title": "short issue title",
      "Legislation Reference": "string or null",
      "Current Section": {
        "ID": "TOC section index or article reference, or null if wholly missing",
        "Title": "document section title, or null if wholly missing",
        "Quote": "problematic direct quote from the document, or null if wholly missing"
      },
      "Problem": "specific documentation problem",
      "Solution": "specific explanation of how the applicant should address the problem",
      "Suggested Fix": {
        "Action": "insert_after_section | append_to_section | replace_text | create_new_section",
        "Insert Location": {
          "Target Section ID": "TOC section index where the frontend should insert the text",
          "Target Section Title": "target document section title",
          "Anchor Quote": "nearby exact quote to anchor insertion, or null",
          "Placement": "before | after | replace | end_of_section | new_section"
        },
        "Insertable Text": "ready-to-paste example text matching the document's style and facts"
      }
    }
  ]
}

Field guidance:
- "exists": true only if ALL task requirements are substantially met.
- "explanation": concise prose verdict — what is present, what is absent, and why.
- "Missing Sections": list only the section/article name and the referenced law for
  each requirement that has NO coverage in the document at all.
  Format each entry as: "Art. X <Topic> (<Law reference>)"
  Example: "Art. 73 Outsourcing Written Agreement (MiCA)", "Art. 30 Governance (DORA)"
  Do NOT include explanations or prose — names and law references only.
- "Incorrect Sections": list sections that exist but are incomplete, ambiguous, or
  non-compliant. Each entry MUST include:
  - "ID": the TOC section index or article reference
  - "Quote": a direct verbatim quote from the document showing the problematic text
  - "Comment": what is wrong or insufficient, and which specific topics or sub-sections
    are required to fix it
- "Issues" is mandatory. For every item in "Missing Sections" and every item in
  "Incorrect Sections", include exactly one corresponding issue object.
- Every issue must include a practical solution and insertable example text. The
  frontend will display "Insertable Text" as grey suggested text that a user can
  insert into the target location.
- Use the full documentation context available through the fetched sections and
  references to draft document-specific text. Reuse the applicant name, service
  scope, terminology, defined systems, governance bodies, and document tone when
  they are known from the documentation. Do not use placeholders like [Company],
  [insert date], or TBD.
- If you need more context to choose the correct insertion point or write the
  example text, call GetSections again for nearby, related, or cross-referenced
  sections before producing the final JSON.
- For missing coverage, choose the most relevant existing section for insertion,
  or use "create_new_section" when the document needs a new standalone section.
- For incorrect coverage, choose "replace_text" only when the quoted text should
  be replaced; otherwise use "append_to_section" or "insert_after_section".
- Both "Missing Sections" and "Incorrect Sections" must be fully populated whenever
  gaps exist — do not leave them empty if issues are found.
"""

    tools_description = """Available tools:
- GetSections(section_indexes) — retrieves the full text of document sections by TOC index.
IMPORTANT: You CANNOT read the document without calling GetSections. The TOC only shows titles and page ranges — the actual content is only accessible via GetSections. You MUST call GetSections on every relevant section before drawing any conclusions."""
    if refs:
        tools_description = (
            f"{tools_description.rstrip()}\n"
            f"{get_instruction_text(instructions, 'task_evaluation.tools_description_with_legislation')}"
        )

    toc_block = render_instruction_template(
        instructions,
        (
            "task_evaluation.toc_block_with_heading"
            if system_prompt_override
            else "task_evaluation.toc_block_inline"
        ),
        toc_str=toc_str,
    )

    if system_prompt_override:
        system_prompt = render_instruction_template(
            instructions,
            "task_evaluation.system_prompt_with_override",
            system_prompt_override=system_prompt_override,
            document_manifest=document_manifest,
            toc_block=toc_block,
            exploratory_summary_block=exploratory_summary_block,
            task_flattened=task_flattened,
            tools_description=tools_description,
            output_format_block=output_format_block,
        )
    else:
        system_prompt = render_instruction_template(
            instructions,
            "task_evaluation.system_prompt_default",
            document_manifest=document_manifest,
            toc_block=toc_block,
            exploratory_summary_block=exploratory_summary_block,
            task_flattened=task_flattened,
            tools_description=tools_description,
            output_format_block=output_format_block,
        )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "GetSections",
                "description": "Retrieves the full text content of multiple document sections based on their TOC index.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section_indexes": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "An array of integer indexes corresponding to the sections in the TOC.",
                        }
                    },
                    "required": ["section_indexes"],
                },
            },
        }
    ]

    if refs:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "GetLegislation",
                    "description": "Retrieves the full text of regulatory references by their ID (e.g. R1, R3, R8). Use this to fetch legislation details before evaluating tasks that cite specific references.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reference_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "An array of reference IDs (e.g. [\"R1\", \"R3\"]) to retrieve.",
                            }
                        },
                        "required": ["reference_ids"],
                    },
                },
            }
        )

    user_msg = get_instruction_text(
        instructions,
        "task_evaluation.user_message_base",
    )
    if refs:
        user_msg = get_instruction_text(
            instructions,
            "task_evaluation.user_message_with_legislation",
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    reasoning_steps: List[ReasoningStep] = []

    for attempt in range(AGENT_MAX_TOOL_CALLS):
        response = await client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            timeout=AGENT_REQUEST_TIMEOUT,
        )

        message = response.choices[0].message
        messages.append(message)

        if message.tool_calls:
            thought = (message.content or "").strip() or None

            all_indexes: List[int] = []
            all_ref_ids: List[str] = []
            for tool_call in message.tool_calls:
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                if tool_call.function.name == "GetSections":
                    raw_indexes = args.get("section_indexes", [])
                    indexes = []
                    for raw_idx in raw_indexes:
                        try:
                            indexes.append(int(raw_idx))
                        except (ValueError, TypeError):
                            pass
                    all_indexes.extend(indexes)

                    found_texts = []
                    for idx in indexes:
                        if 0 <= idx < len(chunks):
                            chunk = chunks[idx]
                            found_texts.append(
                                f"--- Section {idx} ({chunk.get('title', 'Unknown')}) ---\n"
                                f"{chunk.get('text_content', '')}"
                            )
                        else:
                            found_texts.append(f"--- Section {idx} (NOT FOUND) ---")

                    tool_response_text = "\n\n".join(found_texts) if found_texts else "No sections retrieved."
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_response_text,
                        }
                    )

                elif tool_call.function.name == "GetLegislation":
                    ref_ids = args.get("reference_ids", [])
                    all_ref_ids.extend(ref_ids)

                    found_refs = []
                    for ref_id in ref_ids:
                        ref_data = refs.get(ref_id)
                        if ref_data:
                            title = ref_data.get("title", ref_id)
                            text = ref_data.get("text", "")
                            found_refs.append(f"--- [{ref_id}] {title} ---\n{text}")
                        else:
                            found_refs.append(f"--- [{ref_id}] (NOT FOUND) ---")

                    tool_response_text = "\n\n".join(found_refs) if found_refs else "No references found."
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_response_text,
                        }
                    )

            section_titles = [
                chunks[i].get("title") or f"Section {i}"
                for i in all_indexes
                if 0 <= i < len(chunks)
            ]

            reasoning_steps.append(
                ReasoningStep(
                    step=attempt + 1,
                    thought=thought,
                    sections_queried=all_indexes,
                    section_titles=section_titles,
                    references_queried=all_ref_ids,
                )
            )

            if thought:
                logger.info(
                    "[eval] Step %d - thought: %s",
                    attempt + 1,
                    thought[:200] + ("..." if len(thought) > 200 else ""),
                )
            if all_indexes:
                logger.info(
                    "[eval] Step %d - fetching %d section(s): %s",
                    attempt + 1,
                    len(all_indexes),
                    ", ".join(f"{i} ({t})" for i, t in zip(all_indexes, section_titles)),
                )
            if all_ref_ids:
                logger.info(
                    "[eval] Step %d - fetching %d reference(s): %s",
                    attempt + 1,
                    len(all_ref_ids),
                    ", ".join(all_ref_ids),
                )
            if attempt < AGENT_MAX_TOOL_CALLS - 1:
                continue

            logger.warning(
                "[eval] Reached max tool call limit (%d) for task: %s - prompting for final answer",
                AGENT_MAX_TOOL_CALLS,
                task_label,
            )
            messages.append(
                {
                    "role": "user",
                    "content": get_instruction_text(
                        instructions,
                        "task_evaluation.max_tool_call_message",
                    ),
                }
            )
            forced_response = await client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=messages,
                tool_choice="none",
                timeout=AGENT_REQUEST_TIMEOUT,
            )
            message = forced_response.choices[0].message

        final_text = message.content or ""
        exists = False
        explanation = "Failed to parse evaluation response."
        missing_sections: List[str] = []
        incorrect_sections: List[Dict[str, str]] = []
        issues: List[DocumentationIssue] = []

        try:
            (
                exists,
                explanation,
                missing_sections,
                incorrect_sections,
                issues,
            ) = _parse_evaluation_output(final_text)
            if (
                not _issue_suggestions_complete(exists, missing_sections, incorrect_sections, issues)
                and attempt < AGENT_MAX_TOOL_CALLS - 1
            ):
                messages.append({
                    "role": "user",
                    "content": (
                        "Your JSON identifies documentation issues but does not include a complete "
                        "Issues entry for every missing or incorrect section. Return the full corrected "
                        "raw JSON now. Each issue must include Solution, Suggested Fix.Insert Location, "
                        "and Suggested Fix.Insertable Text."
                    ),
                })
                logger.warning(
                    "[eval] Issue suggestions incomplete for task: %s; requesting corrected JSON",
                    task_label,
                )
                continue

        except Exception as exc:
            logger.warning("[eval] Failed to parse agent JSON output: %s. Error: %s", final_text, exc)
            explanation = final_text

        logger.info(
            "[eval] Done - steps: %d | exists: %s | missing: %d | incorrect: %d",
            len(reasoning_steps),
            exists,
            len(missing_sections),
            len(incorrect_sections),
        )

        return TaskEvaluationResult(
            task=task_list,
            exists=exists,
            explanation=explanation,
            missing_sections=missing_sections,
            incorrect_sections=incorrect_sections,
            issues=issues,
            reasoning_steps=reasoning_steps,
        )
