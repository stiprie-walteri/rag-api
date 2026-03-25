import os
import sys
import yaml
import json
import logging
import argparse
from typing import List, Dict, Any
import psycopg
from psycopg.rows import dict_row
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Constants and Env Variables
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")
AGENT_MAX_TOOL_CALLS = int(os.getenv("AGENT_MAX_TOOL_CALLS", "25"))
POSTGRES_DSN = os.getenv("POSTGRES_DSN")

# Ensure required envs
if not OPENROUTER_API_KEY:
    logger.error("OPENROUTER_API_KEY is not set.")
    sys.exit(1)

if not POSTGRES_DSN:
    logger.error("POSTGRES_DSN is not set.")
    sys.exit(1)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

def get_document_chunks(org_id: str, doc_id: str, version_id: str) -> List[Dict[str, Any]]:
    with psycopg.connect(POSTGRES_DSN, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, chunk_level, title, start_page, end_page, text_content 
                FROM document_chunks 
                WHERE organization_id = %s AND document_id = %s AND version_id = %s
                ORDER BY created_at ASC
            """, (org_id, doc_id, version_id))
            return cur.fetchall()

def _format_toc(chunks: List[Dict[str, Any]]) -> str:
    toc_lines = []
    # For TOC, index can be the chunk 'id' or just its list index.
    # Let's use the list index for brevity, mapped to the chunk id internally.
    for i, c in enumerate(chunks):
        title = c.get("title") or "Unnamed Section"
        level_prefix = "  " * (c.get("chunk_level", 1) - 1)
        toc_lines.append(f"{level_prefix}{i}: {title} (Pages {c['start_page']}-{c['end_page']})")
    return "\n".join(toc_lines)

def RunAgent(task_list: List[str], chunks: List[Dict[str, Any]], system_prompt_override: str | None = None):
    task_flattened = "\n".join(f"- {t}" for t in task_list)
    logger.info(f"==> Starting evaluation for task: {task_list[0]}")
    logger.debug(f"Task details: {task_flattened}")

    toc_str = _format_toc(chunks)
    
    if system_prompt_override:
        system_prompt = f"""{system_prompt_override}

TOC:
{toc_str}
---
Task:
Please verify that the document contains information about this:
{task_flattened}

Available tool call functions:
GetSections(section_indexes) - Use this to retrieve the full text content of specific sections by their integer index.
"""
    else:
        system_prompt = f"""
TOC:
{toc_str}
---
Task:
Please verify that the document contains information about this:
{task_flattened}

Available tool call functions:
GetSections(section_indexes) - Use this to retrieve the full text content of specific sections by their integer index.
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
        {"role": "user", "content": "Please begin your analysis and use the GetSections tool to retrieve any necessary text."}
    ]

    for attempt in range(AGENT_MAX_TOOL_CALLS):
        response = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
        
        message = response.choices[0].message
        messages.append(message)
        
        if message.tool_calls:
            for tool_call in message.tool_calls:
                if tool_call.function.name == "GetSections":
                    args = json.loads(tool_call.function.arguments)
                    indexes = args.get("section_indexes", [])
                    logger.info(f"[*] Agent called GetSections for indexes: {indexes}")
                    
                    found_texts = []
                    for idx in indexes:
                        if 0 <= idx < len(chunks):
                            c = chunks[idx]
                            found_texts.append(f"--- Section {idx} ({c['title']}) ---\n{c['text_content']}")
                        else:
                            found_texts.append(f"--- Section {idx} (NOT FOUND) ---")
                    
                    tool_response_text = "\n\n".join(found_texts) if found_texts else "No sections retrieved."
                    
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_response_text
                    })
            continue # Continue loop to let the model process tool output
        else:
            # Model generated a final response text
            logger.info(f"Final evaluation for {task_list[0]}:\n{message.content}\n")
            break
    else:
        logger.warning(f"Agent reached max tool calls ({AGENT_MAX_TOOL_CALLS}) without finishing.")

def main():
    parser = argparse.ArgumentParser(description="Run OpenRouter Agent over Tasks.yaml")
    parser.add_argument("--tasks", default="Tasks.yaml", help="Path to YAML tasks file")
    parser.add_argument("--template-id", default=None, help="Template ID from legislation-templates to use")
    parser.add_argument("--org-id", required=True, help="Organization ID")
    parser.add_argument("--doc-id", required=True, help="Document ID")
    parser.add_argument("--version-id", required=True, help="Version ID")
    args = parser.parse_args()

    task_list_array = []
    system_prompt_override = None

    if args.template_id:
        import glob
        templates_dir = os.getenv("LEGISLATION_TEMPLATES_DIR", "legislation-templates")
        template_data = None
        for file_path in glob.glob(f"{templates_dir}/*.yaml"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    template_id_val = data.get("id", os.path.splitext(os.path.basename(file_path))[0])
                    if template_id_val == args.template_id:
                        template_data = data
                        break
            except Exception:
                pass
                
        if not template_data:
            logger.error(f"Template '{args.template_id}' not found.")
            sys.exit(1)
            
        task_list_array = template_data.get("Tasks", [])
        system_prompt_override = template_data.get("system_prompt")
            
    if not task_list_array and os.path.exists(args.tasks):
        with open(args.tasks, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            task_list_array = data.get("Tasks", [])

    if not task_list_array:
        logger.error("No 'Tasks' found.")
        sys.exit(1)

    logger.info("Fetching document chunks from database...")
    chunks = get_document_chunks(args.org_id, args.doc_id, args.version_id)
    if not chunks:
        logger.error(f"No chunks found for org={args.org_id}, doc={args.doc_id}, version={args.version_id}")
        sys.exit(1)
        
    logger.info(f"Loaded {len(chunks)} sections for evaluation.")

    for i, t_list in enumerate(task_list_array):
        logger.info(f"=== Processing Task {i+1} of {len(task_list_array)} ===")
        RunAgent(t_list, chunks, system_prompt_override=system_prompt_override)

if __name__ == "__main__":
    main()
