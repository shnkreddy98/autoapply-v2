import asyncio

from agents import DocumentTools, ResumeTailorAgent
from utils import read


async def main():
    resume_text = await read("data/shashank_reddy.docx")
    job_description_text = await read("data/meta.md")

    if isinstance(resume_text, str) and isinstance(job_description_text, str):
        doc_tools = DocumentTools("data/shashank_reddy.docx")
        agent = ResumeTailorAgent(doc_tools)
        result = await agent.tailor_resume(job_description_text)
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
