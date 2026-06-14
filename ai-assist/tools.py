import logging
from typing import Optional

from pydantic import BaseModel, Field
from docx import Document

logger = logging.getLogger(__name__)


class ReplaceArgs(BaseModel):
    search_text: str = Field(description="Exact string to find in the document (must appear exactly once)")
    replace_text: str = Field(description="Replacement string")


class DocumentTools:
    """Tools for in-place text replacement in a .docx file while preserving formatting."""

    def __init__(self, file: str):
        self.file = file
        self.document = Document(file)

    async def replace(self, args: ReplaceArgs) -> str:
        count = 0

        def _replace_in_paragraph(paragraph) -> bool:
            if args.search_text not in paragraph.text:
                return False

            new_text = paragraph.text.replace(args.search_text, args.replace_text)

            if paragraph.runs:
                first_run = paragraph.runs[0]
                font_name = first_run.font.name
                font_size = first_run.font.size
                font_bold = first_run.font.bold
                font_italic = first_run.font.italic
                font_color = first_run.font.color.rgb if first_run.font.color.rgb else None

                for run in paragraph.runs:
                    run._element.getparent().remove(run._element)

                new_run = paragraph.add_run(new_text)
                new_run.font.name = font_name
                new_run.font.size = font_size
                new_run.font.bold = font_bold
                new_run.font.italic = font_italic
                if font_color:
                    new_run.font.color.rgb = font_color
            else:
                paragraph.add_run(new_text)

            return True

        for paragraph in self.document.paragraphs:
            if _replace_in_paragraph(paragraph):
                count += 1

        for table in self.document.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        if _replace_in_paragraph(paragraph):
                            count += 1

        if count == 1:
            self.document.save(self.file)
            logger.debug(f"Saved {self.file}")
            return "Successfully replaced"
        elif count == 0:
            return (
                "ERROR: search_text not found in document. "
                "Make sure to use exact text from the resume (no extra spaces or newlines)."
            )
        else:
            return (
                f"ERROR: search_text appears {count} times. "
                "Include more surrounding context (job title, dates, adjacent bullet) "
                "to make it unique and appear exactly once."
            )
