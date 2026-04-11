import os
from pathlib import Path
from string import Template
from typing import Any

import yaml


def get_system_instructions_dir() -> Path:
    return Path(os.getenv("SYSTEM_INSTRUCTIONS_DIR", "system-instructions"))


def load_instruction_set(file_name: str) -> dict[str, Any]:
    file_path = get_system_instructions_dir() / file_name
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"System instructions file not found: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"System instructions file must contain a YAML object: {file_path}")
    return data


def get_instruction_text(instructions: dict[str, Any], dotted_key: str) -> str:
    current: Any = instructions
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(f"Instruction key not found: {dotted_key}")
        current = current[part]

    if not isinstance(current, str):
        raise ValueError(f"Instruction value must be a string: {dotted_key}")
    return current


def render_instruction_template(
    instructions: dict[str, Any],
    dotted_key: str,
    **values: Any,
) -> str:
    template_text = get_instruction_text(instructions, dotted_key)
    normalized_values = {
        key: "" if value is None else value
        for key, value in values.items()
    }
    return Template(template_text).substitute(normalized_values)
