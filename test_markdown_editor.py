import unittest

from app.services.markdown.editor import apply_issue_suggestions, chunk_markdown


class MarkdownEditorTests(unittest.TestCase):
    def test_append_to_target_section(self):
        markdown = "# Programme\n\n## Custody and Administration\n\nClient assets are kept securely.\n\n## Complaints\n\nExisting text."
        issue = {
            "title": "Custody controls incomplete",
            "problem": "Missing segregation detail.",
            "solution": "Append segregation controls.",
            "suggested_fix": {
                "insertable_text": "Client crypto-assets are segregated from NEXUS own assets through separate wallet structures.",
                "insert_location": {
                    "action": "append_to_section",
                    "target_section_title": "Custody and Administration",
                    "anchor_quote": "Client assets are kept securely.",
                    "placement": "end_of_section",
                },
            },
        }

        result = apply_issue_suggestions(markdown, [issue])

        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertIn("separate wallet structures", result["patched_markdown"])
        self.assertLess(
            result["patched_markdown"].index("separate wallet structures"),
            result["patched_markdown"].index("## Complaints"),
        )

    def test_replace_anchor_quote(self):
        markdown = "# Programme\n\n## Custody\n\nClient assets are kept securely."
        issue = {
            "title": "Replace vague custody wording",
            "current_section": {"quote": "Client assets are kept securely."},
            "suggested_fix": {
                "insertable_text": "Client crypto-assets are segregated from NEXUS own assets and reconciled daily.",
                "insert_location": {
                    "action": "replace_text",
                    "target_section_title": "Custody",
                    "anchor_quote": "Client assets are kept securely.",
                    "placement": "replace",
                },
            },
        }

        result = apply_issue_suggestions(markdown, [issue])

        self.assertEqual(result["applied_count"], 1)
        self.assertNotIn("Client assets are kept securely.", result["patched_markdown"])
        self.assertIn("reconciled daily", result["patched_markdown"])

    def test_matches_project_prefixed_section_title(self):
        markdown = "# Programme\n\n## Custody and Administration\n\nClient assets are kept securely.\n\n## Complaints\n\nExisting text."
        issue = {
            "title": "Custody controls incomplete",
            "suggested_fix": {
                "insertable_text": "Client crypto-assets are segregated from NEXUS own assets.",
                "insert_location": {
                    "action": "append_to_section",
                    "target_section_title": "Operations Manual :: Custody and Administration",
                    "placement": "end_of_section",
                },
            },
        }

        result = apply_issue_suggestions(markdown, [issue])

        self.assertEqual(result["applied_count"], 1)
        self.assertLess(
            result["patched_markdown"].index("segregated from NEXUS"),
            result["patched_markdown"].index("## Complaints"),
        )

    def test_chunk_markdown_by_headings(self):
        markdown = "# Programme\n\nIntro.\n\n## Governance\n\nGovernance text.\n\n## Custody\n\nCustody text."

        chunks = chunk_markdown(markdown)

        self.assertEqual([chunk["title"] for chunk in chunks], ["Programme", "Governance", "Custody"])
        self.assertIn("Governance text.", chunks[1]["text"])


if __name__ == "__main__":
    unittest.main()
