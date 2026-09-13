"""Manual tailoring run against local files, outside the queue:

    uv run python main.py path/to/resume.docx path/to/jd.md

The resume docx is edited in place, so pass a copy.
"""

import asyncio
import sys

from agents import DocumentTools, ResumeTailorAgent
from utils import read


async def main(resume_path: str, jd_path: str):
    resume_text = await read(resume_path)
    job_description_text = await read(jd_path)

    if isinstance(resume_text, str) and isinstance(job_description_text, str):
        doc_tools = DocumentTools(resume_path)
        agent = ResumeTailorAgent(doc_tools)
        result = await agent.tailor_resume(job_description_text)
        print(result)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python main.py RESUME.docx JD.md")
    asyncio.run(main(sys.argv[1], sys.argv[2]))
