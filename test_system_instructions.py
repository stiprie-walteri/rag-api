import unittest

from app.services.system_instructions import (
    get_instruction_text,
    load_instruction_set,
    render_instruction_template,
)


class SystemInstructionsTests(unittest.TestCase):
    def test_evaluation_agent_instruction_file_loads(self):
        instructions = load_instruction_set("evaluation-agent.yaml")
        rendered = render_instruction_template(
            instructions,
            "task_evaluation.system_prompt_default",
            document_manifest="",
            toc_block="TOC:\n0: Example",
            exploratory_summary_block="",
            task_flattened="- Example task",
            tools_description="Available tools:\n- Example",
            output_format_block="FORMAT",
        )

        self.assertIn("document verification AI", rendered)
        self.assertIn("Example task", rendered)

    def test_legislation_compare_instruction_file_loads(self):
        instructions = load_instruction_set("legislation-compare.yaml")
        system_message = get_instruction_text(instructions, "compare.system_message")
        user_prompt = render_instruction_template(
            instructions,
            "compare.user_prompt",
            code="145.A.25",
            legislation_markdown="Example legislation",
            submission_text="Example submission",
        )

        self.assertIn("compliance analyst", system_message)
        self.assertIn("145.A.25", user_prompt)
        self.assertIn("Example legislation", user_prompt)


if __name__ == "__main__":
    unittest.main()
