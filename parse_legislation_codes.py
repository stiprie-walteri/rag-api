"""
Aviation Documentation Legislation Code Parser
Extracts legislation codes from organization submission markdown and outputs structured JSON
"""

import re
import json
import sys
from datetime import datetime
from typing import List, Dict, Any, Optional


class LegislationCodeParser:
    """Parser for extracting legislation codes from aviation documentation markdown"""
    
    def __init__(self):
        # Regex patterns for matching legislation codes
        self.code_patterns = [
            # Matches: Part 145.A.30 (a) (b) (c)
            r'(?:Part\s+)?145\.A\.(\d+(?:[a-z])?)\s*(\([a-z]+(?:\)\s*\([a-z]+)*\))?',
            # Matches: AMC 145.A.30 (a)
            r'AMC\s+145\.A\.(\d+(?:[a-z])?)\s*(\([a-z]+(?:\)\s*\([a-z]+)*\))?',
            # Matches: GM 145.A.30 (a)
            r'GM\s+145\.A\.(\d+(?:[a-z])?)\s*(\([a-z]+(?:\)\s*\([a-z]+)*\)?)?',
        ]
        
    def extract_codes_from_text(self, text: str) -> List[str]:
        """
        Extract all legislation codes from a text string.
        
        Args:
            text: Text containing legislation codes
            
        Returns:
            List of normalized legislation codes
        """
        codes = []
        
        # Remove markdown italic markers and clean up text
        text = text.replace('_', '').replace('*', '')
        
        # Handle Part codes with complex subsections
        # Pattern matches: Part 145.A.30 (a) (c) (e) (g) or Part 145.A.70 (a) 2
        part_pattern = r'Part\s+145\.A\.(\d+(?:[a-z])?)\s*([^\-/]+?)(?:\s*[-/]|\s*$)'
        for match in re.finditer(part_pattern, text, re.IGNORECASE):
            base_code = match.group(1)
            subsections_text = match.group(2).strip()
            
            if subsections_text:
                subsection_codes = self._parse_subsections(subsections_text)
                if subsection_codes:
                    for sub in subsection_codes:
                        # Add spaces between parenthesized parts like (c)(1) -> (c) (1)
                        formatted_sub = re.sub(r'\)\s*\(', ') (', sub)
                        codes.append(f"145.A.{base_code} {formatted_sub}")
                else:
                    codes.append(f"145.A.{base_code}")
            else:
                codes.append(f"145.A.{base_code}")
        
        # Handle AMC codes
        amc_pattern = r'AMC\s+145\.A\.(\d+(?:[a-z])?)\s*([^\-/]+?)(?:\s*[-/]|\s*$)'
        for match in re.finditer(amc_pattern, text, re.IGNORECASE):
            base_code = match.group(1)
            subsections_text = match.group(2).strip()
            
            if subsections_text:
                subsection_codes = self._parse_subsections(subsections_text)
                if subsection_codes:
                    for sub in subsection_codes:
                        # Add spaces between parenthesized parts like (c)(1) -> (c) (1)
                        formatted_sub = re.sub(r'\)\s*\(', ') (', sub)
                        codes.append(f"AMC 145.A.{base_code} {formatted_sub}")
                else:
                    codes.append(f"AMC 145.A.{base_code}")
            else:
                codes.append(f"AMC 145.A.{base_code}")
        
        # Handle GM codes
        gm_pattern = r'GM\s+145\.A\.(\d+(?:[a-z])?)\s*([^\-/]+?)(?:\s*[-/]|\s*$)'
        for match in re.finditer(gm_pattern, text, re.IGNORECASE):
            base_code = match.group(1)
            subsections_text = match.group(2).strip()
            
            if subsections_text:
                subsection_codes = self._parse_subsections(subsections_text)
                if subsection_codes:
                    for sub in subsection_codes:
                        # Add spaces between parenthesized parts like (c)(1) -> (c) (1)
                        formatted_sub = re.sub(r'\)\s*\(', ') (', sub)
                        codes.append(f"GM 145.A.{base_code} {formatted_sub}")
                else:
                    codes.append(f"GM 145.A.{base_code}")
            else:
                codes.append(f"GM 145.A.{base_code}")
        
        return list(dict.fromkeys(codes))  # Remove duplicates while preserving order
    
    def _parse_subsections(self, subsections_text: str) -> List[str]:
        """
        Parse subsection notation into individual codes.
        
        Examples:
            "(a) (b) (c)" -> ["(a)", "(b)", "(c)"]
            "(a) 1, 2, 3" -> ["(a)(1)", "(a)(2)", "(a)(3)"]
            "(b) 1,2,7,8" -> ["(b)(1)", "(b)(2)", "(b)(7)", "(b)(8)"]
            "(a)" -> ["(a)"]
        
        Args:
            subsections_text: Text containing subsection notation
            
        Returns:
            List of formatted subsection strings
        """
        result = []
        
        # Clean up the text
        subsections_text = subsections_text.strip()
        
        # Extract all parenthesized sections
        paren_pattern = r'\(([a-z0-9]+)\)'
        paren_matches = re.findall(paren_pattern, subsections_text)
        
        if not paren_matches:
            return result
        
        # Separate letters from numbers
        letters = [m for m in paren_matches if m.isalpha()]
        numbers = [m for m in paren_matches if m.isdigit()]
        
        # If we have both letters and numbers, combine them
        if letters and numbers:
            for letter in letters:
                for number in numbers:
                    result.append(f"({letter})({number})")
        elif letters:
            # Just letters
            for letter in letters:
                result.append(f"({letter})")
        elif numbers:
            # Just numbers (shouldn't happen but handle it)
            for number in numbers:
                result.append(f"({number})")
        
        # Also check for comma-separated numbers after letters
        # Pattern: (a) 1, 2, 3
        for letter in letters:
            pattern = rf'\({letter}\)\s*([\d,\s]+?)(?:\(|\)|$)'
            number_match = re.search(pattern, subsections_text)
            
            if number_match:
                numbers_text = number_match.group(1)
                comma_numbers = re.findall(r'\d+', numbers_text)
                
                if comma_numbers:
                    # Remove the simple letter entry if we found numbered subsections
                    result = [r for r in result if r != f"({letter})"]
                    
                    for num in comma_numbers:
                        code = f"({letter})({num})"
                        if code not in result:
                            result.append(code)
        
        return result
    
    def parse_markdown(self, markdown_path: str) -> Dict[str, Any]:
        """
        Parse the organization submission markdown file and extract legislation codes.
        
        Args:
            markdown_path: Path to the markdown file
            
        Returns:
            Dictionary with parsed structure and extracted codes
        """
        with open(markdown_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        lines = content.split('\n')
        all_sections = []  # Flat list of all sections first
        all_found_codes = set()
        
        i = 0
        while i < len(lines):
            line = lines[i]
            
            # Check for main section headers (e.g., **1.1  CORPORATE...** or **1.2** **QUALITY...**)
            # Handle both formats: **1.1  TEXT** and **1.1** **TEXT**
            section_match = re.match(r'\*\*(\d+\.\d+(?:\.\d+)?)\*\*\s+\*\*(.*?)\*\*', line)
            if not section_match:
                section_match = re.match(r'\*\*(\d+\.\d+(?:\.\d+)?)\s+(.*?)\*\*', line)
            
            if section_match:
                section_number = section_match.group(1)
                title = section_match.group(2).strip()
                
                # Remove trailing periods from title
                title = title.rstrip('.')
                
                # Check if title contains legislation code (e.g., "Part 145.A.75 (c)")
                title_codes = self.extract_codes_from_text(title)
                
                # Look ahead for italic legislation codes in next few lines
                legislation_codes = []
                text_start_idx = i + 1
                i += 1
                
                # Skip empty lines and look for italic legislation text
                for offset in range(10):
                    if i + offset < len(lines):
                        next_line = lines[i + offset].strip()
                        
                        # Skip empty lines
                        if not next_line:
                            continue
                        
                        # Check for italic text with legislation codes
                        if next_line.startswith('_') and ('145.A.' in next_line or 'Part' in next_line or 'AMC' in next_line or 'GM' in next_line):
                            # Combine multiple consecutive lines of italic text
                            full_legislation_text = next_line
                            check_idx = i + offset + 1
                            
                            while check_idx < len(lines):
                                check_line = lines[check_idx].strip()
                                if check_line.startswith('_') and '145.A.' in check_line:
                                    full_legislation_text += ' ' + check_line
                                    check_idx += 1
                                else:
                                    break
                            
                            # Extract codes from the full legislation text
                            codes = self.extract_codes_from_text(full_legislation_text)
                            legislation_codes.extend(codes)
                            text_start_idx = check_idx
                            break
                        
                        # If we hit non-empty, non-italic text, stop looking
                        if not next_line.startswith('_'):
                            text_start_idx = i + offset
                            break
                
                # Extract section text (until next section or end)
                section_text_lines = []
                text_idx = text_start_idx
                while text_idx < len(lines):
                    text_line = lines[text_idx].strip()
                    
                    # Stop if we hit another section header
                    if re.match(r'\*\*(\d+\.\d+(?:\.\d+)?)\*\*\s+\*\*(.*?)\*\*', text_line) or \
                       re.match(r'\*\*(\d+\.\d+(?:\.\d+)?)\s+(.*?)\*\*', text_line):
                        break
                    
                    # Stop if we hit a table or other structural element
                    if text_line.startswith('|') and len(section_text_lines) > 0:
                        break
                    
                    # Add non-empty lines
                    if text_line and not text_line.startswith('|'):
                        section_text_lines.append(text_line)
                    
                    text_idx += 1
                    
                    # Limit text collection to reasonable amount
                    if len(section_text_lines) > 50:
                        break
                
                # Join text lines
                section_text = ' '.join(section_text_lines)
                
                # Combine codes from title and italic text
                all_codes = title_codes + legislation_codes
                all_codes = list(dict.fromkeys(all_codes))  # Remove duplicates
                
                # Add to all found codes
                all_found_codes.update(all_codes)
                
                section_data = {
                    "section_number": section_number,
                    "title": title,
                    "legislation_codes": all_codes,
                    "text": section_text
                }
                
                all_sections.append(section_data)
            
            i += 1
        
        # Now organize sections into hierarchy (nest subsections)
        sections = self._nest_subsections(all_sections)
        
        # Create the output structure
        result = {
            "document_metadata": {
                "organization": "JET SUPPORT",
                "approval_number": "UK.145.01306",
                "parsed_date": datetime.now().strftime("%Y-%m-%d")
            },
            "sections": sections,
            "all_found_codes": sorted(list(all_found_codes)),
            "statistics": {
                "total_sections": len(sections),
                "sections_with_codes": len([s for s in sections if s["legislation_codes"]]),
                "total_unique_codes": len(all_found_codes)
            }
        }
        
        return result
    
    def _nest_subsections(self, flat_sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Nest subsections under their parent sections.
        
        For example:
        - 1.3.1 should be nested under 1.3
        - 1.4.1, 1.4.2 should be nested under 1.4
        
        Args:
            flat_sections: Flat list of all sections
            
        Returns:
            Hierarchical list with subsections nested
        """
        result = []
        
        for section in flat_sections:
            section_num = section["section_number"]
            parts = section_num.split('.')
            
            # Check if this is a subsection (has 3 or more parts, e.g., 1.3.1)
            if len(parts) >= 3:
                # Find parent section (e.g., 1.3 for 1.3.1)
                parent_num = '.'.join(parts[:2])
                
                # Find the parent in result list
                parent_found = False
                for parent in result:
                    if parent["section_number"] == parent_num:
                        # Add this as a subsection
                        if "subsections" not in parent:
                            parent["subsections"] = []
                        parent["subsections"].append(section)
                        parent_found = True
                        break
                
                # If parent not found, add as top-level section
                if not parent_found:
                    section["subsections"] = []
                    result.append(section)
            else:
                # This is a top-level section
                section["subsections"] = []
                result.append(section)
        
        return result
    
    def save_json(self, data: Dict[str, Any], output_path: str):
        """
        Save the parsed data to a JSON file.
        
        Args:
            data: Parsed data dictionary
            output_path: Path where JSON file will be saved
        """
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"JSON saved to: {output_path}")


def main():
    """Main function to run the parser"""
    if len(sys.argv) != 3:
        print("Usage: python parse_legislation_codes.py <input_md_path> <output_json_path>")
        sys.exit(1)
    
    markdown_path = sys.argv[1]
    output_path = sys.argv[2]
    
    parser = LegislationCodeParser()
    
    print(f"Parsing legislation codes from: {markdown_path}")
    result = parser.parse_markdown(markdown_path)
    
    # Display statistics
    print(f"\n=== Parsing Complete ===")
    print(f"Total sections found: {result['statistics']['total_sections']}")
    print(f"Sections with legislation codes: {result['statistics']['sections_with_codes']}")
    print(f"Total unique legislation codes: {result['statistics']['total_unique_codes']}")
    
    # Save to JSON
    parser.save_json(result, output_path)
    
    print(f"\n✓ Legislation codes parsed and saved to {output_path}")
    
    # Print a sample of found codes
    if result['all_found_codes']:
        print(f"\nSample of found codes (first 10):")
        for code in result['all_found_codes'][:10]:
            print(f"  - {code}")


if __name__ == "__main__":
    main()
