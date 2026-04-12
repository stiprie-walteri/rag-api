import unittest
from app.services.markdown.editor import apply_issue_suggestions, resolve_issue_location

class IndexingTests(unittest.TestCase):
    def test_apply_suggestions_returns_indices(self):
        markdown = "# Title\n\nSome text here."
        issue = {
            "title": "Fix text",
            "suggested_fix": {
                "insertable_text": "Better text",
                "insert_location": {
                    "action": "replace_text",
                    "anchor_quote": "text here",
                },
            },
        }
        
        result = apply_issue_suggestions(markdown, [issue])
        app = result["applications"][0]
        
        self.assertEqual(app["status"], "applied")
        self.assertEqual(app["start_index"], 14)
        self.assertEqual(app["end_index"], 23)
        self.assertIn("Some Better text.", result["patched_markdown"])

    def test_resolve_issue_location(self):
        markdown = "# Title\n\nSection one content.\n\n## Section Two\n\nSection two content."
        issue = {
            "title": "Add to section two",
            "suggested_fix": {
                "insertable_text": "New content",
                "insert_location": {
                    "action": "append_to_section",
                    "target_section_title": "Section Two",
                },
            },
        }
        
        resolved = resolve_issue_location(markdown, issue)
        location = resolved["suggested_fix"]["insert_location"]
        
        # Section Two ends at the end of the string
        self.assertEqual(location["start_index"], len(markdown))
        self.assertEqual(location["end_index"], len(markdown))

if __name__ == "__main__":
    unittest.main()
