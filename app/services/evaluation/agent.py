import os
import json
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from openai import AsyncOpenAI
from dotenv import load_dotenv
from json_repair import repair_json

load_dotenv()
logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")
AGENT_MAX_TOOL_CALLS = int(os.getenv("AGENT_MAX_TOOL_CALLS", "25"))
AGENT_REQUEST_TIMEOUT = int(os.getenv("AGENT_REQUEST_TIMEOUT", "120"))

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


class ReasoningStep(BaseModel):
    step: int
    thought: Optional[str] = None          # model's reasoning text before the tool call
    sections_queried: List[int] = []        # section indexes it decided to fetch
    section_titles: List[str] = []          # human-readable titles for those indexes
    references_queried: List[str] = []     # legislation reference IDs fetched (e.g. R1, R3)


class TaskEvaluationResult(BaseModel):
    legislation_id: Optional[str] = None
    legislation_name: Optional[str] = None
    task: List[str]
    exists: bool
    explanation: str
    missing_sections: List[str] = []
    incorrect_sections: List[Dict[str, str]] = []
    reasoning_steps: List[ReasoningStep] = []


def _format_toc(chunks: List[Dict[str, Any]]) -> str:
    toc_lines = []
    for i, c in enumerate(chunks):
        title = c.get("title") or "Unnamed Section"
        level_prefix = "  " * (c.get("chunk_level", 1) - 1)
        toc_lines.append(f"{level_prefix}{i}: {title} (Pages {c.get('start_page', '?')}-{c.get('end_page', '?')})")
    return "\n".join(toc_lines)


async def evaluate_task_with_agent(
    task_list: List[str],
    chunks: List[Dict[str, Any]],
    system_prompt_override: Optional[str] = None,
    references: Optional[Dict[str, Dict[str, str]]] = None,
) -> TaskEvaluationResult:
    try:
        client = get_openrouter_client()
    except ValueError as e:
        return TaskEvaluationResult(
            task=task_list,
            exists=False,
            explanation=f"Agent setup failed: {str(e)}"
        )

    task_flattened = "\n".join(f"- {t}" for t in task_list)
    toc_str = _format_toc(chunks)
    refs = references or {}

    task_label = task_list[0][:60] + ("..." if len(task_list[0]) > 60 else "")
    logger.info("[eval] Starting — task: %s | chunks available: %d", task_label, len(chunks))

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
- Both "Missing Sections" and "Incorrect Sections" must be fully populated whenever
  gaps exist — do not leave them empty if issues are found.
"""

    tools_description = """Available tools:
- GetSections(section_indexes) — retrieves the full text of document sections by TOC index.
IMPORTANT: You CANNOT read the document without calling GetSections. The TOC only shows titles and page ranges — the actual content is only accessible via GetSections. You MUST call GetSections on every relevant section before drawing any conclusions."""
    if refs:
        tools_description += "\n- GetLegislation(reference_ids) — retrieves regulatory reference texts by ID (e.g. R1, R3). Fetch references before evaluating tasks that cite them."

    if system_prompt_override:
        system_prompt = f"""{system_prompt_override}

==============================================================================
DOCUMENT TABLE OF CONTENTS
==============================================================================
{toc_str}

==============================================================================
CURRENT TASK
==============================================================================
Please verify that the document contains information about this:
{task_flattened}

==============================================================================
TOOLS
==============================================================================
{tools_description}

{output_format_block}"""
    else:
        system_prompt = f"""
You are a document verification AI. Your job is to verify if the provided task components are explicitly mentioned or covered within the document.

Do not use emojis anywhere in your response.

CRITICAL: The TOC below shows section titles only. You CANNOT assess the document content from titles alone.
You MUST call GetSections to read the actual text of any section before making a judgement.
Never conclude a section is missing or incorrect without first fetching and reading it.

TOC:
{toc_str}
---
Task:
Please verify that the document contains information about this:
{task_flattened}

{tools_description}

{output_format_block}"""

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
                            "description": "An array of integer indexes corresponding to the sections in the TOC."
                        }
                    },
                    "required": ["section_indexes"],
                },
            }
        }
    ]

    if refs:
        tools.append({
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
                            "description": "An array of reference IDs (e.g. [\"R1\", \"R3\"]) to retrieve."
                        }
                    },
                    "required": ["reference_ids"],
                },
            }
        })

    user_msg = "Please begin your analysis, use GetSections to retrieve the text, and output the final JSON evaluation."
    if refs:
        user_msg = "Please begin your analysis. Use GetLegislation to fetch the regulatory references cited by the task, use GetSections to retrieve the document text, and output the final JSON evaluation."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg}
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
                            c = chunks[idx]
                            found_texts.append(f"--- Section {idx} ({c.get('title', 'Unknown')}) ---\n{c.get('text_content', '')}")
                        else:
                            found_texts.append(f"--- Section {idx} (NOT FOUND) ---")

                    tool_response_text = "\n\n".join(found_texts) if found_texts else "No sections retrieved."
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_response_text
                    })

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
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_response_text
                    })

            section_titles = [
                chunks[i].get("title") or f"Section {i}"
                for i in all_indexes
                if 0 <= i < len(chunks)
            ]

            step = ReasoningStep(
                step=attempt + 1,
                thought=thought,
                sections_queried=all_indexes,
                section_titles=section_titles,
                references_queried=all_ref_ids,
            )
            reasoning_steps.append(step)

            if thought:
                logger.info(
                    "[eval] Step %d — thought: %s",
                    attempt + 1,
                    thought[:200] + ("..." if len(thought) > 200 else ""),
                )
            if all_indexes:
                logger.info(
                    "[eval] Step %d — fetching %d section(s): %s",
                    attempt + 1,
                    len(all_indexes),
                    ", ".join(f"{i} ({t})" for i, t in zip(all_indexes, section_titles)),
                )
            if all_ref_ids:
                logger.info(
                    "[eval] Step %d — fetching %d reference(s): %s",
                    attempt + 1,
                    len(all_ref_ids),
                    ", ".join(all_ref_ids),
                )
            if attempt < AGENT_MAX_TOOL_CALLS - 1:
                continue

            # Last attempt exhausted — force a final answer without tools
            logger.warning(
                "[eval] Reached max tool call limit (%d) for task: %s — prompting for final answer",
                AGENT_MAX_TOOL_CALLS,
                task_label,
            )
            messages.append({
                "role": "user",
                "content": (
                    "You have reached the maximum number of tool calls. "
                    "Please provide your final JSON evaluation now without calling any more tools."
                ),
            })
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

            # Use json-repair to fix any syntax slips from the LLM
            repaired = repair_json(cleaned)
            data = json.loads(repaired)

            exists = bool(data.get("exists", False))
            explanation = str(data.get("explanation", "")) or str(data.get("explanation", final_text))

            ms_val = data.get("Missing Sections", data.get("missing_sections", []))
            if isinstance(ms_val, list):
                missing_sections = [str(x) for x in ms_val if str(x).strip()]

            is_val = data.get("Incorrect Sections", data.get("incorrect_sections", []))
            if isinstance(is_val, list):
                cleaned_sections: List[Dict[str, str]] = []
                for item in is_val:
                    if not isinstance(item, dict):
                        continue
                    cleaned_sections.append({
                        "ID": str(item.get("ID", "")).strip(),
                        "Quote": str(item.get("Quote", "")).strip(),
                        "Comment": str(item.get("Comment", "")).strip(),
                    })
                incorrect_sections = [x for x in cleaned_sections if x.get("ID") or x.get("Quote") or x.get("Comment")]

        except Exception as e:
            logger.warning("[eval] Failed to parse agent JSON output: %s. Error: %s", final_text, e)
            explanation = final_text

        logger.info(
            "[eval] Done — steps: %d | exists: %s | missing: %d | incorrect: %d",
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
