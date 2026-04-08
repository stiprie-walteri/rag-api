import logging
import os
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)


def get_templates_dir() -> Path:
    return Path(os.getenv("LEGISLATION_TEMPLATES_DIR", "legislation-templates"))


def load_legislation_templates() -> list[dict[str, Any]]:
    templates_dir = get_templates_dir()
    templates: list[dict[str, Any]] = []

    if not templates_dir.exists() or not templates_dir.is_dir():
        return templates

    for file_path in sorted(templates_dir.glob("*.yaml")):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Failed to load template %s: %s", file_path, exc)
            continue

        if not isinstance(data, dict):
            logger.warning("Template %s did not contain a YAML object.", file_path)
            continue

        template_id = str(data.get("id", file_path.stem)).strip()
        if not template_id:
            logger.warning("Template %s has an empty id and was skipped.", file_path)
            continue

        data["id"] = template_id
        data["name"] = str(data.get("name", file_path.stem)).strip() or file_path.stem
        data["_file_path"] = str(file_path)
        templates.append(data)

    return templates


def get_legislation_template(template_id: str) -> dict[str, Any] | None:
    wanted = template_id.strip()
    if not wanted:
        return None

    for template in load_legislation_templates():
        if template["id"] == wanted:
            return template
    return None


def validate_template_ids(template_ids: list[str] | None) -> list[str]:
    if not template_ids:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    available_ids = {template["id"] for template in load_legislation_templates()}

    for raw_template_id in template_ids:
        template_id = str(raw_template_id).strip()
        if not template_id:
            continue
        if template_id in seen:
            continue
        if template_id not in available_ids:
            raise ValueError(f"Legislation template {template_id} not found.")
        normalized.append(template_id)
        seen.add(template_id)

    return normalized
