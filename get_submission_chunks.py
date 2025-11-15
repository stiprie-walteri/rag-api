import json
import re
from pathlib import Path

SUBMISSION_FILE = "parsed_legislation_codes.json"


def normalize_code(s):
    """Same normalisation as elsewhere: strip and remove all whitespace."""
    return re.sub(r"\s+", "", s.strip())


def parse_section_id(section_id):
    """
    Same parser as the legislation side, reused for matching.

    Returns (prefix, base_id, inline_tokens).
    """
    s = section_id.strip()

    m = re.match(r'^(AMC\w*|GM\d*|GM)\s*(.*)$', s)
    if m:
        prefix = m.group(1)
        rest = m.group(2).strip()
    else:
        prefix = ""
        rest = s

    if not rest:
        return prefix, "", []

    first_special = len(rest)
    for ch in ("(", ";"):
        idx = rest.find(ch)
        if idx != -1 and idx < first_special:
            first_special = idx

    base_id = rest[:first_special].strip()
    inline_part = rest[first_special:].strip()

    inline_tokens = re.findall(r'\([^()]+\)', inline_part)
    return prefix, base_id, inline_tokens


def codes_match(query_code, leg_code):
    """
    Match a query legislation code from metrics against a legislation code
    attached to a submission section.

    Rules:
      - If query has parentheses: require full exact match (ignoring spaces).
      - If query is a base code (no parentheses):
          * match any code (reg / AMC / GM) that shares the same base id,
            regardless of prefix (e.g. "145.A.25" matches "AMC 145.A.25 (a)").
    """
    q = str(query_code)
    l = str(leg_code)

    pq, bq, tq = parse_section_id(q)
    pl, bl, tl = parse_section_id(l)

    # If either base is missing, fall back on strict normalized equality
    if not bq or not bl:
        return normalize_code(q) == normalize_code(l)

    # Query is a specific subsection: must match exactly
    if tq:
        return normalize_code(q) == normalize_code(l)

    # Query is a main/base code (no parentheses):
    # ignore prefix, match on base id only
    return bq == bl


def load_submission_json():
    path = Path(SUBMISSION_FILE)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def section_matches_codes(section, codes):
    leg_codes = section.get("legislation_codes", []) or []
    for lc in leg_codes:
        for qc in codes:
            if codes_match(qc, lc):
                return True
    return False


def build_section_block(section, parent_prefix=""):
    """
    Build a markdown block for a section (and its subsections).

    ## <section_number> <title>

    <text>
    """
    blocks = []

    sec_no = section.get("section_number")
    title = section.get("title") or ""
    text = section.get("text") or ""

    heading_parts = []
    if sec_no:
        heading_parts.append(str(sec_no))
    if title:
        heading_parts.append(str(title))

    heading = " ".join(heading_parts).strip()
    if heading:
        blocks.append(f"## {heading}")
    else:
        # Fallback if no section number/title
        blocks.append("## Submission Section")

    if text.strip():
        blocks.append("")
        blocks.append(text.strip())

    # Recurse into subsections
    subsections = section.get("subsections", []) or []
    for sub in subsections:
        if not isinstance(sub, dict):
            continue
        sub_block = build_section_block(sub, parent_prefix=sec_no or parent_prefix)
        if sub_block.strip():
            blocks.append("")
            blocks.append(sub_block)

    return "\n".join(blocks).strip()


def get_submission_by_codes(codes):
    """
    Given a list of legislation codes (e.g. ["145.A.30", "AMC 145.A.30"]),
    return a markdown string concatenating every section/subsection from
    parsed_legislation_codes.json that is relevant to ANY of those codes.

    Logic:
      - If a top-level section's legislation_codes match any code:
            include the ENTIRE section subtree (its text + all subsections).
      - If a top-level section does not match, but a nested subsection has
        legislation_codes that match, include only that subsection subtree.
    """
    data = load_submission_json()
    sections = data.get("sections", []) or []

    matched_blocks = []
    seen_keys = set()  # to avoid duplicates: (section_number, title)

    def add_block_for_section(section):
        key = (section.get("section_number"), section.get("title"))
        if key in seen_keys:
            return
        seen_keys.add(key)
        block = build_section_block(section)
        if block:
            matched_blocks.append(block)

    for sec in sections:
        if not isinstance(sec, dict):
            continue

        # Case 1: section itself matches -> include entire subtree
        if section_matches_codes(sec, codes):
            add_block_for_section(sec)
            continue

        # Case 2: check subsections individually
        subsections = sec.get("subsections", []) or []
        for sub in subsections:
            if not isinstance(sub, dict):
                continue
            if section_matches_codes(sub, codes):
                add_block_for_section(sub)

    return "\n\n\n".join(matched_blocks).strip()
