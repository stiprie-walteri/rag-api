import json
import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from json_repair import repair_json
from openai import AsyncOpenAI
from pydantic import BaseModel

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
    thought: Optional[str] = None
    sections_queried: List[int] = []
    section_titles: List[str] = []
    references_queried: List[str] = []


class TaskEvaluationResult(BaseModel):
    legislation_id: Optional[str] = None
    legislation_name: Optional[str] = None
    task: List[str]
    status: str = "completed"
    exists: bool
    explanation: str
    missing_sections: List[str] = []
    incorrect_sections: List[Dict[str, str]] = []
    reasoning_steps: List[ReasoningStep] = []


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

    output_format_block = get_instruction_text(
        instructions,
        "task_evaluation.output_format_block",
    )
    tools_description = get_instruction_text(
        instructions,
        "task_evaluation.tools_description_base",
    )
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

        try:
            cleaned = final_text.strip()
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

            exists = bool(data.get("exists", False))
            explanation = str(data.get("explanation", "")) or str(data.get("explanation", final_text))

            ms_val = data.get("Missing Sections", data.get("missing_sections", []))
            if isinstance(ms_val, list):
                missing_sections = [str(item) for item in ms_val if str(item).strip()]

            is_val = data.get("Incorrect Sections", data.get("incorrect_sections", []))
            if isinstance(is_val, list):
                cleaned_sections: List[Dict[str, str]] = []
                for item in is_val:
                    if not isinstance(item, dict):
                        continue
                    cleaned_sections.append(
                        {
                            "ID": str(item.get("ID", "")).strip(),
                            "Quote": str(item.get("Quote", "")).strip(),
                            "Comment": str(item.get("Comment", "")).strip(),
                        }
                    )
                incorrect_sections = [
                    item
                    for item in cleaned_sections
                    if item.get("ID") or item.get("Quote") or item.get("Comment")
                ]

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
            reasoning_steps=reasoning_steps,
        )
