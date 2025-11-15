#!/usr/bin/env python3
import json
import argparse
import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


def normalize_code(s: str) -> str:
    """
    Normalize a code string so that formatting differences (spaces, etc.)
    do not matter for comparison.

    Example:
      "145.A.25 (c) (1)" -> "145.A.25(c)(1)"
      " 145.A.25(c)(1) " -> "145.A.25(c)(1)"
    """
    return re.sub(r"\s+", "", s.strip())


def load_legislation_unique_sections(path: Path) -> Dict[str, Any]:
    """
    Load unique_sections_legislation.json and return:
      - base_ids_raw: list of base ids as in the file (e.g. "145.A.25")
      - base_to_subsections_raw: mapping base_id -> list of subsection strings
      - norm_main_ids: set of normalized base ids
      - norm_all_leg_codes: set of all normalized codes (bases + subsections)
    """
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("unique_sections_legislation.json must contain a list at root.")

    base_ids_raw: List[str] = []
    base_to_subsections_raw: Dict[str, List[str]] = {}
    norm_main_ids: Set[str] = set()
    norm_all_leg_codes: Set[str] = set()

    for entry in data:
        if not isinstance(entry, dict):
            continue

        base_id = entry.get("id")
        if not isinstance(base_id, str):
            continue

        base_ids_raw.append(base_id)
        base_norm = normalize_code(base_id)
        norm_main_ids.add(base_norm)
        norm_all_leg_codes.add(base_norm)

        subsections = entry.get("subsections", [])
        if not isinstance(subsections, list):
            subsections = []

        raw_list: List[str] = []
        for sub in subsections:
            if not isinstance(sub, str):
                continue
            raw_list.append(sub)
            sub_norm = normalize_code(sub)
            norm_all_leg_codes.add(sub_norm)

        base_to_subsections_raw[base_id] = raw_list

    return {
        "base_ids_raw": base_ids_raw,
        "base_to_subsections_raw": base_to_subsections_raw,
        "norm_main_ids": norm_main_ids,
        "norm_all_leg_codes": norm_all_leg_codes,
    }


def parse_submission_codes(path: Path) -> Tuple[List[str], Set[str]]:
    """
    Load parsed_legislation_codes.json and return:
      - list of raw codes from "all_found_codes"
      - set of normalized codes
    """
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    codes = data.get("all_found_codes", [])
    if not isinstance(codes, list):
        raise ValueError("parsed_legislation_codes.json must contain 'all_found_codes' as a list.")

    raw_codes = [c for c in codes if isinstance(c, str)]
    norm_codes = {normalize_code(c) for c in raw_codes}

    return raw_codes, norm_codes


def compute_metrics(
    legislation_info: Dict[str, Any],
    submission_raw_codes: List[str],
    submission_norm_codes: Set[str],
) -> Dict[str, List[str]]:
    """
    Reverse the perspective:

    - Iterate over legislation (main codes + subsections) and ask:
        * which main codes are found in the submission?
        * which main codes are not found in the submission?
        * which subsections are found in the submission?
        * which subsections are not found in the submission?

    - Then iterate over submission codes and ask:
        * which codes are not present anywhere in legislation?
    """
    base_ids_raw: List[str] = legislation_info["base_ids_raw"]
    base_to_subsections_raw: Dict[str, List[str]] = legislation_info["base_to_subsections_raw"]
    norm_main_ids: Set[str] = legislation_info["norm_main_ids"]
    norm_all_leg_codes: Set[str] = legislation_info["norm_all_leg_codes"]

    # MAIN CODES: found / not found
    all_main_codes_found: List[str] = []
    all_main_codes_not_found: List[str] = []

    # SUBSECTIONS: found / not found
    found_subsections_set: Set[str] = set()
    not_found_subsections_list: List[str] = []

    for base_id in base_ids_raw:
        base_norm = normalize_code(base_id)
        subsections_raw = base_to_subsections_raw.get(base_id, [])

        # A main code is considered "found" if:
        #   - the base itself is explicitly in the submission, OR
        #   - any of its subsections is present in the submission.
        base_present = base_norm in submission_norm_codes
        subsection_present = any(
            normalize_code(sub) in submission_norm_codes for sub in subsections_raw
        )

        if base_present or subsection_present:
            all_main_codes_found.append(base_id)
        else:
            all_main_codes_not_found.append(base_id)

        # Subsection coverage
        for sub in subsections_raw:
            sub_norm = normalize_code(sub)
            if sub_norm in submission_norm_codes:
                found_subsections_set.add(sub)
            else:
                not_found_subsections_list.append(sub)

    # All subsections found (deduplicated)
    all_subsections_found = sorted(found_subsections_set)

    # SUBMISSION CODES NOT IN LEGISLATION
    all_sections_not_in_legislation: List[str] = []
    for raw in submission_raw_codes:
        norm = normalize_code(raw)
        if norm not in norm_all_leg_codes and norm not in norm_main_ids:
            all_sections_not_in_legislation.append(raw)

    metrics = {
        "all_main_codes_found": sorted(all_main_codes_found),
        "all_main_codes_not_found": sorted(all_main_codes_not_found),
        "all_subsections_found": all_subsections_found,
        "all_subsections_not_found": sorted(not_found_subsections_list),
        "all_sections_not_in_legislation": sorted(all_sections_not_in_legislation),
    }

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare legislation (unique_sections_legislation.json) against "
            "a submission (parsed_legislation_codes.json) and compute coverage metrics."
        )
    )
    parser.add_argument(
        "--legislation",
        default="unique_sections_legislation.json",
        help="Path to unique_sections_legislation.json (default: unique_sections_legislation.json).",
    )
    parser.add_argument(
        "--output",
        default="legislation_comparison_metrics.json",
        help="Path to write comparison metrics JSON (default: legislation_comparison_metrics.json).",
    )

    args = parser.parse_args()

    legislation_path = Path(args.legislation)
    submission_path = Path("../parsed_legislation_codes.json")
    output_path = Path(args.output)

    legislation_info = load_legislation_unique_sections(legislation_path)
    submission_raw_codes, submission_norm_codes = parse_submission_codes(submission_path)
    metrics = compute_metrics(legislation_info, submission_raw_codes, submission_norm_codes)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
