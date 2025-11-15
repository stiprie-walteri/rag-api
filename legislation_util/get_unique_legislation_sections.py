#!/usr/bin/env python3
import json
import argparse
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


def parse_section_id(section_id: str) -> Tuple[str, str, List[str]]:
    """
    Parse a raw section id into (prefix, base_id, inline_tokens).

    Examples:
        "145.A.30"          -> ("", "145.A.30", [])
        "145.A.30(g)"       -> ("", "145.A.30", ["(g)"])
        "145.A.30(c);(ca)"  -> ("", "145.A.30", ["(c)", "(ca)"])
        "GM3 145.A.30(e)"   -> ("GM3", "145.A.30", ["(e)"])
        "GM1 145.A.30(cb)"  -> ("GM1", "145.A.30", ["(cb)"])
        "AMC 145.A.10"      -> ("AMC", "145.A.10", [])
        "AMC1 145.A.15(b)"  -> ("AMC1", "145.A.15", ["(b)"])
    """
    s = section_id.strip()

    # Extract optional prefix like AMC, AMC1, GM, GM3, etc.
    m = re.match(r'^(AMC\w*|GM\d*|GM)\s*(.*)$', s)
    if m:
        prefix = m.group(1)
        rest = m.group(2).strip()
    else:
        prefix = ""
        rest = s

    if not rest:
        return prefix, "", []

    # Base id: up to first '(' or ';'
    first_special = len(rest)
    for ch in ("(", ";"):
        idx = rest.find(ch)
        if idx != -1 and idx < first_special:
            first_special = idx

    base_id = rest[:first_special].strip()
    inline_part = rest[first_special:].strip()

    # Extract all "(...)" tokens from the inline part
    inline_tokens = re.findall(r'\([^()]+\)', inline_part)

    return prefix, base_id, inline_tokens


def format_token(raw_id: str) -> str:
    """
    Turn a node 'id' into a '(token)' part for the path.

    Rules:
      - If id already looks like '(a)', '(b)', etc., keep as is.
      - Otherwise, strip trailing '.', wrap remaining in parentheses.
        e.g. '1.' -> '(1)', '1' -> '(1)'.
    """
    value = raw_id.strip()
    if value.startswith("(") and value.endswith(")"):
        return value
    value = value.rstrip(".")
    return f"({value})"


def gather_paths_from_node(
    node: Dict[str, Any],
    root_label: str,
    ancestor_tokens: List[str],
    paths: List[str],
) -> None:
    """
    Recursively collect fully-qualified subsection paths from this node.

    Each path is a string like:
      root_label + " " + "(a) (1) (i)"
    """
    content = node.get("content", [])
    if not isinstance(content, list):
        return

    for child in content:
        if not isinstance(child, dict):
            continue

        child_id = child.get("id")
        if isinstance(child_id, str):
            token = format_token(child_id)
            tokens = ancestor_tokens + [token]
            path = root_label + "".join(" " + t for t in tokens)
            paths.append(path)
            gather_paths_from_node(child, root_label, tokens, paths)
        else:
            # No id at this level; still recurse for any deeper ids
            gather_paths_from_node(child, root_label, ancestor_tokens, paths)


def unique_preserve_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def process_legislation(data: Any) -> List[Dict[str, Any]]:
    """
    Group all sections by their base id (first id without AMC/GM),
    then for each base id produce:

      {
        "id": "<base_id>",
        "subsections": [
          "145.A.30 (g)",
          "145.A.30 (c)",
          "145.A.30 (c) (1)",
          "GM3 145.A.30 (e)",
          "GM1 145.A.30 (cb)",
          ...
        ]
      }
    """
    if isinstance(data, list):
        nodes = data
    elif isinstance(data, dict):
        nodes = [data]
    else:
        raise ValueError("Root JSON must be a list or an object.")

    # Group top-level nodes by base id
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order_of_bases: List[str] = []

    for node in nodes:
        if not isinstance(node, dict):
            continue
        sec_id = node.get("id")
        if not isinstance(sec_id, str):
            continue

        prefix, base_id, _ = parse_section_id(sec_id)
        if not base_id:
            continue

        if base_id not in groups:
            groups[base_id] = []
            order_of_bases.append(base_id)
        groups[base_id].append(node)

    results: List[Dict[str, Any]] = []

    for base_id in order_of_bases:
        group_nodes = groups[base_id]
        all_paths: List[str] = []

        for node in group_nodes:
            sec_id = str(node.get("id", "")).strip()
            prefix, parsed_base_id, inline_tokens = parse_section_id(sec_id)

            # Root label:
            # - Main regulation: base_id
            # - AMC/GM: full "prefix base_id"
            if prefix:
                root_label = f"{prefix} {parsed_base_id}"
            else:
                root_label = parsed_base_id

            # Path for the node itself, if it has inline tokens
            if inline_tokens:
                self_path = root_label + "".join(" " + t for t in inline_tokens)
                all_paths.append(self_path)

            # Now recurse into children, starting from inline tokens as ancestors
            gather_paths_from_node(node, root_label, inline_tokens, all_paths)

        all_paths = unique_preserve_order(all_paths)

        results.append(
            {
                "id": base_id,
                "subsections": all_paths,
            }
        )

    return results


def main() -> None:
    input_path = Path("legislation.json")
    output_path = Path("unique_sections_legislation.json")

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    result = process_legislation(data)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
