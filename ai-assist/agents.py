import logging

from agent import Agent
from env import MODEL
from models import Resume, TailoredResume
from prompts import SYSTEM_PROMPT_PARSE, SYSTEM_PROMPT_TAILOR
from tools import DocumentTools, ReplaceArgs

logger = logging.getLogger(__name__)


def _replace_tool_schema() -> dict:
    from pydantic import BaseModel

    schema = ReplaceArgs.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": "replace",
            "description": "Replace the exact string in the resume document",
            "parameters": schema,
            "strict": True,
        },
    }


class ResumeTailorAgent(Agent):
    """
    Tailors a resume .docx to a specific job description.

    Makes exactly 2 surgical replacements (summary + weakest bullet) using
    the replace tool, then returns a TailoredResume with before/after scores.

    Usage:
        doc_tools = DocumentTools("path/to/resume.docx")
        agent = ResumeTailorAgent(doc_tools)
        result = await agent.tailor_resume(job_description_text)
    """

    def __init__(self, document_tools: DocumentTools, model: str = MODEL):
        self._replace_count = 0
        original_replace = document_tools.replace

        async def tracked_replace(args):
            result = await original_replace(args)
            self._replace_count += 1
            return result

        super().__init__(
            system_prompt=SYSTEM_PROMPT_TAILOR,
            response_format=TailoredResume,
            model=model,
            tools=[_replace_tool_schema()],
            tool_functions={"replace": tracked_replace},
            tool_schemas={"replace": ReplaceArgs},
            temperature=0.7,
        )
        self.document = document_tools.document

    def _tool_choice(self) -> str:
        # Stop offering the tool once 2 replacements have been made
        return "none" if self._replace_count >= 2 else "auto"

    async def tailor_resume(self, job_description: str) -> TailoredResume:
        resume_text = "\n".join(
            p.text for p in self.document.paragraphs if p.text.strip()
        )
        query = f"""Resume starts here
---
{resume_text}
---
Resume ends here

Job description starts here
---
{job_description}
---
Job description ends here

Analyze the resume against the job description and create a tailored version using the tools you have available.
"""
        result = await self.run(query, max_iterations=10)
        return result.output


class ResumeParserAgent(Agent):
    """
    Parses raw resume text into a structured Resume object.

    Usage:
        agent = ResumeParserAgent()
        resume = await agent.parse_resume(resume_text)
    """

    def __init__(self, model: str = MODEL):
        super().__init__(
            system_prompt=SYSTEM_PROMPT_PARSE,
            response_format=Resume,
            model=model,
            temperature=0.1,
            max_tokens=4000,
        )

    async def parse_resume(self, resume_text: str) -> Resume:
        query = f"""Resume starts here
---
{resume_text}
---
Resume ends here

Parse this resume and extract all fields.
"""
        result = await self.run(query, max_iterations=5)
        return result.output
