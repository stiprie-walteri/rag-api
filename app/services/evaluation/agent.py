import os
import json
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")
AGENT_MAX_TOOL_CALLS = int(os.getenv("AGENT_MAX_TOOL_CALLS", "25"))

# Ensure client is only initialized if key is present to prevent startup crashes when unused
_client: Optional[OpenAI] = None
def get_openrouter_client() -> OpenAI:
    global _client
    if not _client:
        if not OPENROUTER_API_KEY:
            raise ValueError("OPENROUTER_API_KEY is not set.")
        _client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )
    return _client

class TaskEvaluationResult(BaseModel):
    task: List[str]
    exists: bool
    explanation: str
    correctness_score: int = 0
    missing_sections: List[str] = []
    incorrect_sections: List[Dict[str, str]] = []

def _format_toc(chunks: List[Dict[str, Any]]) -> str:
    toc_lines = []
    for i, c in enumerate(chunks):
        title = c.get("title") or "Unnamed Section"
        level_prefix = "  " * (c.get("chunk_level", 1) - 1)
        toc_lines.append(f"{level_prefix}{i}: {title} (Pages {c.get('start_page', '?')}-{c.get('end_page', '?')})")
    return "\n".join(toc_lines)

def evaluate_task_with_agent(task_list: List[str], chunks: List[Dict[str, Any]]) -> TaskEvaluationResult:
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
    
    system_prompt = f"""
You are a document verification AI. Your job is to verify if the provided task components are explicitly mentioned or covered within the document.

TOC:
{toc_str}
---
Task:
Please verify that the document contains information about this:
{task_flattened}

Available tool call functions:
GetSections(section_indexes) - Use this to retrieve the full text content of specific sections by their integer index.

IMPORTANT:
- Use GetSections to fetch the exact sections you rely on.
- When you have finished gathering information, output a raw JSON object as your final response.
- DO NOT wrap the JSON in markdown (no ```json fences).

Your final JSON MUST match this structure exactly:
{{
  "exists": true | false,
  "explanation": "string",
  "Correctness Score": 0-100,
  "Missing Sections": ["string", "string"],
  "Incorrect Sections": [
    {{
      "ID": "string (section/chunk id)",
      "Quote": "string (specific quote from the section)",
      "Comment": "string (what should be improved)"
    }}
  ]
}}

Guidance:
- "Correctness Score" is an integer percent (0-100) reflecting overall coverage and accuracy for the task.
- "Missing Sections" lists section titles/codes that should exist for the task but are not present in the document.
- "Incorrect Sections" lists sections that appear relevant but contain incorrect, conflicting, or insufficient information. Use the section index from TOC as the "ID" when applicable.
"""

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

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Please begin your analysis, use GetSections to retrieve the text, and output the final JSON evaluation."}
    ]

    for attempt in range(AGENT_MAX_TOOL_CALLS):
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        
        message = response.choices[0].message
        messages.append(message)
        
        if message.tool_calls:
            for tool_call in message.tool_calls:
                if tool_call.function.name == "GetSections":
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                        
                    indexes = args.get("section_indexes", [])
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
            continue 
        else:
            final_text = message.content or ""
            # Try to parse the final JSON response
            exists = False
            explanation = "Failed to parse evaluation response."
            correctness_score = 0
            missing_sections: List[str] = []
            incorrect_sections: List[Dict[str, str]] = []

            try:
                # Basic cleanup in case the model added markdown blocks
                if final_text.startswith("```json"):
                    final_text = final_text[7:]
                if final_text.startswith("```"):
                    final_text = final_text[3:]
                if final_text.endswith("```"):
                    final_text = final_text[:-3]

                data = json.loads(final_text.strip())
                exists = bool(data.get("exists", False))
                explanation = str(data.get("explanation", "")) or str(data.get("explanation", final_text))

                # Support the new fields (use exact keys as requested, but tolerate snake_case too)
                score_val = data.get("Correctness Score", data.get("correctness_score", 0))
                try:
                    correctness_score = int(score_val)
                except Exception:
                    correctness_score = 0
                correctness_score = max(0, min(100, correctness_score))

                ms_val = data.get("Missing Sections", data.get("missing_sections", []))
                if isinstance(ms_val, list):
                    missing_sections = [str(x) for x in ms_val if str(x).strip()]

                is_val = data.get("Incorrect Sections", data.get("incorrect_sections", []))
                if isinstance(is_val, list):
                    # Expect list[object] with ID/Quote/Comment
                    cleaned: List[Dict[str, str]] = []
                    for item in is_val:
                        if not isinstance(item, dict):
                            continue
                        cleaned.append({
                            "ID": str(item.get("ID", "")).strip(),
                            "Quote": str(item.get("Quote", "")).strip(),
                            "Comment": str(item.get("Comment", "")).strip(),
                        })
                    incorrect_sections = [x for x in cleaned if x.get("ID") or x.get("Quote") or x.get("Comment")]
            except Exception as e:
                logger.warning(f"Failed to parse agent JSON output: {final_text}. Error: {e}")
                explanation = final_text

            return TaskEvaluationResult(
                task=task_list,
                exists=exists,
                explanation=explanation,
                correctness_score=correctness_score,
                missing_sections=missing_sections,
                incorrect_sections=incorrect_sections,
            )

    return TaskEvaluationResult(
        task=task_list,
        exists=False,
        explanation=f"Evaluation failed: Reached maximum tool calls limit ({AGENT_MAX_TOOL_CALLS}).",
        correctness_score=0,
        missing_sections=[],
        incorrect_sections=[],
    )
