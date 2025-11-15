#!/usr/bin/env python3
import json
import re
from pathlib import Path


# Hard-coded inputs. Change these as needed.
LEGISLATION_FILE = "legislation.json"
QUERY_CODE = "AMC2 145.A.15"  # e.g. "145.A.10", "AMC 145.A.10", "145.A.25(c)"


def normalize_code(s):
    """Remove all whitespace so formatting does not matter for comparison."""
    return re.sub(r"\s+", "", s.strip())


def parse_section_id(section_id):
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


def format_token(raw_id):
    """
    Turn a node 'id' into a '(token)' part for the path.

    Rules:
      - If id already looks like '(a)', '(b)', etc., keep as is.
      - Otherwise, strip trailing '.', wrap remaining in parentheses.
    """
    value = raw_id.strip()
    if value.startswith("(") and value.endswith(")"):
        return value
    value = value.rstrip(".")
    return f"({value})"


def gather_paths_from_node(node, root_label, ancestor_tokens, paths):
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


def unique_preserve_order(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def load_legislation():
    """
    Load legislation.json and build indices:

      - nodes: list of top-level nodes
      - id_to_node: id string -> node dict
      - base_groups: base_id -> list of nodes (main + AMC/GM etc)
      - base_to_all_paths: base_id -> list of all subsection paths
      - id_to_paths: exact id -> list of subsection paths for that node only
      - all_paths: list of all subsection paths (for subsection-prefix queries)
    """
    path = Path(LEGISLATION_FILE)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        nodes = data
    elif isinstance(data, dict):
        nodes = [data]
    else:
        raise ValueError("legislation.json root must be a list or an object.")

    id_to_node = {}
    base_groups = {}
    base_order = []

    # Group by base id and index by exact id
    for node in nodes:
        if not isinstance(node, dict):
            continue
        sec_id = node.get("id")
        if not isinstance(sec_id, str):
            continue

        id_to_node[sec_id] = node
        prefix, base_id, _ = parse_section_id(sec_id)
        if not base_id:
            continue

        if base_id not in base_groups:
            base_groups[base_id] = []
            base_order.append(base_id)
        base_groups[base_id].append(node)

    base_to_all_paths = {}
    id_to_paths = {}
    all_paths = []

    # Build paths
    for base_id in base_order:
        group_nodes = base_groups[base_id]
        base_paths = []

        for node in group_nodes:
            sec_id = node.get("id", "").strip()
            prefix, parsed_base_id, inline_tokens = parse_section_id(sec_id)

            # Root label:
            # - main regulation: "145.A.10"
            # - AMC/GM: full id "AMC 145.A.10"
            if prefix:
                root_label = f"{prefix} {parsed_base_id}"
            else:
                root_label = parsed_base_id

            # Path for node itself, if it has inline tokens in its own id
            node_paths = []

            if inline_tokens:
                self_path = root_label + "".join(" " + t for t in inline_tokens)
                base_paths.append(self_path)
                node_paths.append(self_path)

            # Now recurse into children, starting from inline tokens as ancestors
            gather_paths_from_node(node, root_label, inline_tokens, base_paths)
            gather_paths_from_node(node, root_label, inline_tokens, node_paths)

            # Store per-id paths
            id_to_paths[sec_id] = unique_preserve_order(node_paths)

        base_paths = unique_preserve_order(base_paths)
        base_to_all_paths[base_id] = base_paths
        all_paths.extend(base_paths)

    all_paths = unique_preserve_order(all_paths)

    return {
        "nodes": nodes,
        "id_to_node": id_to_node,
        "base_groups": base_groups,
        "base_to_all_paths": base_to_all_paths,
        "id_to_paths": id_to_paths,
        "all_paths": all_paths,
    }


def get_subsections_for_code(code, leg):
    """
    Given any code, return:
      {
        "id": "<input_code>",
        "subsections": [ ... ]
      }

    Rules:
      - If code is a main/base code like "145.A.10":
          return all subsections from that base, including AMC/GM variants.
      - If code is "AMC 145.A.10" (or GM, AMC1, etc.):
          return only the subsections under that specific AMC/GM node.
      - If code includes parentheses, e.g. "145.A.25(c)" or "AMC 145.A.10(a)":
          return all deeper subsections whose normalized path starts
          with the normalized code and is longer.
    """
    base_to_all_paths = leg["base_to_all_paths"]
    id_to_paths = leg["id_to_paths"]
    all_paths = leg["all_paths"]

    norm_code = normalize_code(code)
    prefix_q, base_q, inline_tokens_q = parse_section_id(code)

    # 1) If the code includes parentheses -> treat as a subsection path.
    if inline_tokens_q:
        out = []
        for path in all_paths:
            if normalize_code(path).startswith(norm_code) and len(normalize_code(path)) > len(norm_code):
                out.append(path)
        return {
            "id": code,
            "subsections": out,
        }

    # 2) No parentheses: either main base code or AMC/GM top-level.

    # Try AMC/GM exact match first.
    for sec_id, paths in id_to_paths.items():
        if normalize_code(sec_id) == norm_code:
            # If it's AMC/GM (has prefix), return only its own paths.
            pre, _, _ = parse_section_id(sec_id)
            if pre:
                return {
                    "id": code,
                    "subsections": paths,
                }

    # Main/base code: return everything for that base, including AMC/GM
    if base_q in base_to_all_paths:
        return {
            "id": code,
            "subsections": base_to_all_paths[base_q],
        }

    # Fallback: no match
    return {
        "id": code,
        "subsections": [],
    }


if __name__ == "__main__":
    legislation = load_legislation()
    result = get_subsections_for_code(QUERY_CODE, legislation)
    print(json.dumps(result, ensure_ascii=False, indent=2))
