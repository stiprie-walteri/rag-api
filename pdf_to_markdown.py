"""
PDF to Markdown Converter for Aviation Documentation
Converts org_submission.pdf to markdown format for further analysis
"""

import pymupdf4llm
import sys

def convert_pdf_to_markdown(pdf_path: str, output_path: str) -> None:
    """
    Convert a PDF file to Markdown format.
    
    Args:
        pdf_path: Path to the input PDF file
        output_path: Path where the markdown file will be saved
    """
    print(f"Converting {pdf_path} to markdown...")
    
    # Convert PDF to markdown
    md_text = pymupdf4llm.to_markdown(pdf_path)
    
    # Save to file
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md_text)
    
    print(f"Conversion complete! Markdown saved to {output_path}")
    print(f"Total characters: {len(md_text)}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python pdf_to_markdown.py <input_pdf_path> <output_md_path>")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    output_path = sys.argv[2]
    convert_pdf_to_markdown(pdf_path, output_path)
