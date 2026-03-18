import sys
import json
from pathlib import Path

from pdf_chunking import chunk_pdf

pdf_path = Path("c:/Users/Jekabs/Junction/rag-api/org_submission.pdf")
with open(pdf_path, "rb") as f:
    pdf_bytes = f.read()

print("Chunking PDF...")
try:
    chunks = chunk_pdf(pdf_bytes)
    print(f"Extracted {len(chunks)} chunks.")
    if chunks:
        print("First chunk preview:")
        print(json.dumps(chunks[0], indent=2))
except Exception as e:
    print(f"Failed to chunk PDF: {e}")
    sys.exit(1)
