import json
import unittest

from app.services.evaluation.agent import _issue_suggestions_complete, _parse_evaluation_output


class EvaluationAgentOutputParsingTests(unittest.TestCase):
    def test_parse_insertable_issue_suggestion(self):
        payload = {
            "exists": False,
            "explanation": "Complaints handling is missing and custody wording is incomplete.",
            "Missing Sections": ["Art. 62(2)(l) Complaints Handling (MiCA)"],
            "Incorrect Sections": [
                {
                    "ID": "12",
                    "Quote": "Client assets are kept securely.",
                    "Comment": "The custody section does not explain segregation controls.",
                }
            ],
            "Issues": [
                {
                    "Type": "missing_section",
                    "Title": "Complaints handling procedure missing",
                    "Legislation Reference": "Art. 62(2)(l) MiCA",
                    "Current Section": None,
                    "Problem": "The document does not describe complaint intake or resolution.",
                    "Solution": "Add an operational complaints-handling section with intake, acknowledgement, investigation, escalation, final response timing, ADR, records, and root-cause analysis.",
                    "Suggested Fix": {
                        "Action": "create_new_section",
                        "Insert Location": {
                            "Target Section ID": "11",
                            "Target Section Title": "Client Communications",
                            "Anchor Quote": "NEXUS provides clients with clear communications.",
                            "Placement": "after",
                        },
                        "Insertable Text": "### Complaints Handling\n\nNEXUS will accept complaints in writing, by email, through the client portal, and in person.",
                    },
                },
                {
                    "Type": "incorrect_section",
                    "Title": "Custody segregation controls incomplete",
                    "Legislation Reference": "Art. 75 MiCA",
                    "Current Section": {
                        "ID": "12",
                        "Title": "Custody and Administration",
                        "Quote": "Client assets are kept securely.",
                    },
                    "Problem": "The section is too generic and does not describe segregation controls.",
                    "Solution": "Append concrete wallet, ledger, reconciliation, and control descriptions to the custody section.",
                    "Suggested Fix": {
                        "Action": "append_to_section",
                        "Insert Location": {
                            "Target Section ID": "12",
                            "Target Section Title": "Custody and Administration",
                            "Anchor Quote": "Client assets are kept securely.",
                            "Placement": "after",
                        },
                        "Insertable Text": "Client crypto-assets will be segregated from NEXUS own assets through separate wallet structures and internal ledger accounts.",
                    },
                },
            ],
        }

        exists, explanation, missing, incorrect, issues = _parse_evaluation_output(json.dumps(payload))

        self.assertFalse(exists)
        self.assertIn("Complaints handling", explanation)
        self.assertEqual(missing, ["Art. 62(2)(l) Complaints Handling (MiCA)"])
        self.assertEqual(incorrect[0]["ID"], "12")
        self.assertEqual(len(issues), 2)
        self.assertTrue(issues[0].issue_id.startswith("issue-1-"))
        self.assertEqual(issues[0].issue_type, "missing_section")
        self.assertEqual(issues[0].suggested_fix.insert_location.action, "create_new_section")
        self.assertEqual(issues[0].suggested_fix.insert_location.target_section_id, "11")
        self.assertIn("### Complaints Handling", issues[0].suggested_fix.insertable_text)
        self.assertEqual(issues[1].current_section.id, "12")
        self.assertEqual(issues[1].suggested_fix.insert_location.placement, "after")
        self.assertTrue(_issue_suggestions_complete(exists, missing, incorrect, issues))

    def test_incomplete_issue_suggestions_require_retry(self):
        payload = {
            "exists": False,
            "explanation": "Complaints handling is missing.",
            "Missing Sections": ["Art. 62(2)(l) Complaints Handling (MiCA)"],
            "Incorrect Sections": [],
            "Issues": [
                {
                    "Type": "missing_section",
                    "Title": "Complaints handling procedure missing",
                    "Problem": "The document does not describe complaint intake or resolution.",
                }
            ],
        }

        exists, _, missing, incorrect, issues = _parse_evaluation_output(json.dumps(payload))

        self.assertFalse(_issue_suggestions_complete(exists, missing, incorrect, issues))

    def test_failed_result_without_issue_lists_requires_retry(self):
        payload = {
            "exists": False,
            "explanation": "Complaints handling is missing.",
            "Missing Sections": [],
            "Incorrect Sections": [],
            "Issues": [],
        }

        exists, _, missing, incorrect, issues = _parse_evaluation_output(json.dumps(payload))

        self.assertFalse(_issue_suggestions_complete(exists, missing, incorrect, issues))


if __name__ == "__main__":
    unittest.main()
