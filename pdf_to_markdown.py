"""
PDF to Markdown Converter for Aviation Documentation.

Uses `pymupdf4llm` when available and falls back to plain PyMuPDF text
extraction so the API can still start in environments where the markdown
helper package is not installed.
"""

import sys

def convert_pdf_to_markdown(pdf_path: str, output_path: str) -> None:
    """
    Convert a PDF file to Markdown format.
    
    Args:
        pdf_path: Path to the input PDF file
        output_path: Path where the markdown file will be saved
    """
    print(f"Converting {pdf_path} to markdown...")

    try:
        import pymupdf4llm  # type: ignore
    except ModuleNotFoundError:
        pymupdf4llm = None

    if pymupdf4llm is not None:
        md_text = pymupdf4llm.to_markdown(pdf_path)
    else:
        try:
            import pymupdf  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PDF conversion requires either 'pymupdf4llm' or 'pymupdf' to be installed."
            ) from exc

        doc = pymupdf.open(pdf_path)
        try:
            pages: list[str] = []
            for index, page in enumerate(doc, start=1):
                text = page.get_text("text").strip()
                if not text:
                    continue
                pages.append(f"## Page {index}\n\n{text}")
            md_text = "\n\n".join(pages)
        finally:
            doc.close()

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
