import unittest
from unittest.mock import patch

from app.services.pdf.chunking import chunk_pdf, extract_fallback_chunks


class _FakePage:
    def __init__(self, text: str):
        self._text = text

    def get_text(self, mode: str):
        if mode != "text":
            raise AssertionError(f"Unexpected text mode: {mode}")
        return self._text


class _FakeDoc:
    def __init__(self, page_texts: list[str]):
        self._pages = [_FakePage(text) for text in page_texts]

    def __len__(self):
        return len(self._pages)

    def load_page(self, index: int):
        return self._pages[index]

    def get_toc(self, simple: bool = False):
        return []


class ChunkingFallbackTests(unittest.TestCase):
    def test_extract_fallback_chunks_groups_pages(self):
        doc = _FakeDoc([
            "Alpha section text.",
            "Beta section text.",
            "Gamma section text.",
        ])

        chunks = extract_fallback_chunks(doc, max_chars=40)

        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0]["title"], "Page 1")
        self.assertEqual(chunks[1]["start_page"], 2)
        self.assertEqual(chunks[2]["end_page"], 3)

    @patch("app.services.pdf.chunking.fitz.open")
    def test_chunk_pdf_uses_page_fallback_when_structural_chunking_unavailable(self, mock_open):
        mock_open.return_value = _FakeDoc([
            "Intro page text.",
            "Detailed content on page two.",
        ])

        chunks = chunk_pdf(b"fake pdf bytes")

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["title"], "Pages 1-2")
        self.assertIn("Detailed content", chunks[0]["text"])

    @patch("app.services.pdf.chunking.extract_fallback_chunks")
    @patch("app.services.pdf.chunking.extract_chunks_from_toc")
    @patch("app.services.pdf.chunking.get_fallback_toc")
    @patch("app.services.pdf.chunking.fitz.open")
    def test_chunk_pdf_uses_page_fallback_when_structural_chunks_are_empty(
        self,
        mock_open,
        mock_get_fallback_toc,
        mock_extract_chunks,
        mock_extract_fallback,
    ):
        doc = _FakeDoc(["Page one text."])
        mock_open.return_value = doc
        mock_get_fallback_toc.return_value = [[1, "Section", 1, {}]]
        mock_extract_chunks.return_value = [{"title": "Section", "text": "   "}]
        mock_extract_fallback.return_value = [{"title": "Page 1", "text": "Page one text."}]

        chunks = chunk_pdf(b"fake pdf bytes")

        self.assertEqual(chunks, [{"title": "Page 1", "text": "Page one text."}])
        mock_extract_fallback.assert_called_once_with(doc)


if __name__ == "__main__":
    unittest.main()