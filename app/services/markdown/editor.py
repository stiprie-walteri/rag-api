import hashlib
import re
from typing import Any, Mapping


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _to_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _lookup(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_title(value: str | None) -> str:
    if not value:
        return ""
    normalized = re.sub(r"[*_`#\[\]()>]", "", value)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().casefold()


def _candidate_titles(value: str | None) -> list[str]:
    title = (value or "").strip()
    if not title:
        return []
    candidates = [title]
    if "::" in title:
        candidates.append(title.rsplit("::", 1)[-1].strip())
    return [candidate for candidate in candidates if candidate]


def _canonical_issue_id(issue: Mapping[str, Any], index: int) -> str:
    raw_id = _text(_lookup(issue, "issue_id", "Issue ID", "id"))
    if raw_id:
        return raw_id
    suggested_fix = _to_dict(_lookup(issue, "suggested_fix", "Suggested Fix") or {})
    fingerprint = "|".join(
        [
            _text(_lookup(issue, "issue_type", "Type", "type")),
            _text(_lookup(issue, "title", "Title")),
            _text(_lookup(issue, "problem", "Problem")),
            _text(_lookup(suggested_fix, "insertable_text", "Insertable Text"))[:200],
        ]
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:12]
    return f"issue-{index + 1}-{digest}"


def normalize_issue(issue: Any, index: int = 0) -> dict[str, Any]:
    raw = _to_dict(issue)
    suggested_fix = _to_dict(_lookup(raw, "suggested_fix", "Suggested Fix", "fix", "Fix") or {})
    insert_location = _to_dict(
        _lookup(suggested_fix, "insert_location", "Insert Location", "location", "Location") or {}
    )
    current_section = _to_dict(_lookup(raw, "current_section", "Current Section") or {})

    normalized = {
        "issue_id": _canonical_issue_id(raw, index),
        "issue_type": _text(_lookup(raw, "issue_type", "Type", "type")) or "documentation_issue",
        "title": _text(_lookup(raw, "title", "Title")),
        "legislation_reference": _text(
            _lookup(raw, "legislation_reference", "Legislation Reference", "reference", "Reference")
        ) or None,
        "current_section": (
            {
                "id": _text(_lookup(current_section, "id", "ID")) or None,
                "title": _text(_lookup(current_section, "title", "Title")) or None,
                "quote": _text(_lookup(current_section, "quote", "Quote")) or None,
            }
            if current_section
            else None
        ),
        "problem": _text(_lookup(raw, "problem", "Problem")),
        "solution": _text(_lookup(raw, "solution", "Solution")),
        "suggested_fix": {
            "insertable_text": _text(_lookup(suggested_fix, "insertable_text", "Insertable Text")),
            "insert_location": {
                "action": (
                    _text(_lookup(insert_location, "action", "Action"))
                    or _text(_lookup(suggested_fix, "action", "Action"))
                    or "insert"
                ),
                "target_section_id": (
                    _text(_lookup(insert_location, "target_section_id", "Target Section ID", "id", "ID"))
                    or None
                ),
                "target_section_title": (
                    _text(_lookup(insert_location, "target_section_title", "Target Section Title", "title", "Title"))
                    or None
                ),
                "anchor_quote": (
                    _text(_lookup(insert_location, "anchor_quote", "Anchor Quote", "anchor_text", "Anchor Text"))
                    or None
                ),
                "placement": _text(_lookup(insert_location, "placement", "Placement")) or "after",
            },
        },
    }
    return normalized


def normalize_issues(issues: list[Any]) -> list[dict[str, Any]]:
    return [normalize_issue(issue, index) for index, issue in enumerate(issues)]


def chunk_markdown(markdown: str) -> list[dict[str, Any]]:
    matches = list(HEADING_RE.finditer(markdown))
    if not matches:
        text = markdown.strip()
        return [
            {
                "level": 1,
                "title": "Document",
                "start_page": 1,
                "end_page": max(1, markdown.count("\n") + 1),
                "text": text,
            }
        ] if text else []

    chunks: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        next_match = matches[index + 1] if index + 1 < len(matches) else None
        start = match.start()
        end = next_match.start() if next_match else len(markdown)
        text = markdown[start:end].strip()
        start_line = markdown.count("\n", 0, start) + 1
        end_line = markdown.count("\n", 0, end) + 1
        chunks.append(
            {
                "level": len(match.group(1)),
                "title": match.group(2).strip(),
                "start_page": start_line,
                "end_page": end_line,
                "text": text,
            }
        )
    return chunks


def _find_heading_bounds(markdown: str, title: str | None) -> tuple[int, int, str] | None:
    normalized_targets = [_normalize_title(candidate) for candidate in _candidate_titles(title)]
    normalized_targets = [candidate for candidate in normalized_targets if candidate]
    if not normalized_targets:
        return None

    matches = list(HEADING_RE.finditer(markdown))
    best: tuple[int, int, str] | None = None
    for index, match in enumerate(matches):
        heading_level = len(match.group(1))
        heading_title = match.group(2).strip()
        normalized_heading = _normalize_title(heading_title)
        if not any(
            normalized_heading == normalized_target or normalized_target in normalized_heading
            for normalized_target in normalized_targets
        ):
            continue

        end = len(markdown)
        for next_match in matches[index + 1 :]:
            if len(next_match.group(1)) <= heading_level:
                end = next_match.start()
                break
        best = (match.start(), end, heading_title)
        if normalized_heading in normalized_targets:
            return best
    return best


def _find_exact_or_normalized(markdown: str, quote: str | None) -> tuple[int, int, str] | None:
    if not quote:
        return None

    exact = markdown.find(quote)
    if exact >= 0:
        return exact, exact + len(quote), "anchor_quote"

    normalized_quote = re.sub(r"\s+", " ", quote).strip()
    if not normalized_quote or normalized_quote == quote:
        return None

    pattern = re.escape(normalized_quote)
    pattern = pattern.replace(r"\ ", r"\s+")
    match = re.search(pattern, markdown)
    if match:
        return match.start(), match.end(), "normalized_anchor_quote"
    return None


def _format_insertion(markdown: str, index: int, text: str) -> str:
    prefix = "\n\n" if index > 0 and not markdown[:index].endswith("\n\n") else ""
    suffix = "\n\n" if index < len(markdown) and not markdown[index:].startswith("\n\n") else ""
    return f"{prefix}{text.strip()}{suffix}"


def _insert_at(markdown: str, index: int, text: str) -> str:
    return markdown[:index] + _format_insertion(markdown, index, text) + markdown[index:]


def _replace_range(markdown: str, start: int, end: int, text: str) -> str:
    replacement = text.strip()
    return markdown[:start] + replacement + markdown[end:]


def apply_issue_suggestions(markdown: str, issues: list[Any]) -> dict[str, Any]:
    patched = markdown
    applications: list[dict[str, Any]] = []
    normalized_issues = normalize_issues(issues)

    for issue in normalized_issues:
        fix = issue["suggested_fix"]
        location = fix["insert_location"]
        action = location["action"]
        insertable_text = fix["insertable_text"].strip()
        issue_id = issue["issue_id"]

        if not insertable_text:
            applications.append(
                {
                    "issue_id": issue_id,
                    "title": issue["title"],
                    "action": action,
                    "status": "failed",
                    "reason": "Suggestion did not include insertable text.",
                    "match_strategy": None,
                }
            )
            continue

        if insertable_text in patched:
            applications.append(
                {
                    "issue_id": issue_id,
                    "title": issue["title"],
                    "action": action,
                    "status": "skipped",
                    "reason": "Insertable text already exists in the document.",
                    "match_strategy": "dedupe",
                }
            )
            continue

        current_section = issue.get("current_section") or {}
        anchor_match = _find_exact_or_normalized(
            patched,
            location.get("anchor_quote") or current_section.get("quote"),
        )
        heading_bounds = _find_heading_bounds(patched, location.get("target_section_title"))

        try:
            if action == "replace_text":
                if not anchor_match:
                    raise ValueError("Could not find anchor quote to replace.")
                start, end, strategy = anchor_match
                patched = _replace_range(patched, start, end, insertable_text)
                applications.append(
                    {
                        "issue_id": issue_id,
                        "title": issue["title"],
                        "action": action,
                        "status": "applied",
                        "reason": None,
                        "match_strategy": strategy,
                    }
                )
                continue

            if action in {"append_to_section", "insert_after_section", "create_new_section"} and heading_bounds:
                _, section_end, _ = heading_bounds
                patched = _insert_at(patched, section_end, insertable_text)
                applications.append(
                    {
                        "issue_id": issue_id,
                        "title": issue["title"],
                        "action": action,
                        "status": "applied",
                        "reason": None,
                        "match_strategy": "target_section_title",
                    }
                )
                continue

            if anchor_match:
                start, end, strategy = anchor_match
                insert_at = start if location.get("placement") == "before" else end
                patched = _insert_at(patched, insert_at, insertable_text)
                applications.append(
                    {
                        "issue_id": issue_id,
                        "title": issue["title"],
                        "action": action,
                        "status": "applied",
                        "reason": None,
                        "match_strategy": strategy,
                    }
                )
                continue

            if action == "create_new_section":
                patched = _insert_at(patched, len(patched), insertable_text)
                applications.append(
                    {
                        "issue_id": issue_id,
                        "title": issue["title"],
                        "action": action,
                        "status": "applied",
                        "reason": None,
                        "match_strategy": "document_end",
                    }
                )
                continue

            raise ValueError("Could not find target section title or anchor quote.")
        except ValueError as exc:
            applications.append(
                {
                    "issue_id": issue_id,
                    "title": issue["title"],
                    "action": action,
                    "status": "failed",
                    "reason": str(exc),
                    "match_strategy": None,
                }
            )

    return {
        "patched_markdown": patched,
        "applications": applications,
        "applied_count": sum(1 for item in applications if item["status"] == "applied"),
        "skipped_count": sum(1 for item in applications if item["status"] == "skipped"),
        "failed_count": sum(1 for item in applications if item["status"] == "failed"),
    }
