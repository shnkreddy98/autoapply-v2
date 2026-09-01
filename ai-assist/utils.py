import json
import logging
import os
import yaml

from docx import Document
from docx.oxml.ns import qn
from pypdf import PdfReader
from typing import Union

logger = logging.getLogger(__name__)


def docx_text(file: str) -> str:
    """All text in a .docx, in document order, tables included.

    `Document(...).paragraphs` is not enough: a resume template keeps employers,
    titles, dates and the whole education block in one-row tables, and reading only
    paragraphs yields bare bullets under empty headings — the model then answers
    that the candidate has no education. Walking the body keeps table rows in place
    under the heading they belong to.
    """
    doc = Document(file)
    parts = []
    for child in doc.element.body:
        if child.tag == qn("w:p"):
            text = "".join(t.text or "" for t in child.iter(qn("w:t")))
            if text.strip():
                parts.append(text)
        elif child.tag == qn("w:tbl"):
            for row in child.iter(qn("w:tr")):
                cells = [
                    "".join(t.text or "" for t in cell.iter(qn("w:t")))
                    for cell in row.iter(qn("w:tc"))
                ]
                line = "\t".join(c for c in cells if c.strip())
                if line.strip():
                    parts.append(line)
    return "\n".join(parts)


async def read(file: str) -> Union[str, dict]:
    """Read a file and return its contents. Supports .json, .yaml, .pdf, .docx, and plain text."""
    with open(file, "r") as f:
        if file.endswith(".json"):
            return json.load(f)
        elif file.endswith(".yaml"):
            return yaml.safe_load(f)

    if file.endswith(".pdf"):
        return PdfReader(file).pages[0].extract_text()

    if file.endswith(".docx"):
        return docx_text(file)

    with open(file, "r") as f:
        return f.read()


async def write(file: str, data: str) -> bool:
    """Write data to a file, creating parent directories as needed."""
    os.makedirs("/".join(file.split("/")[:-1]), exist_ok=True)
    try:
        with open(file, "w", encoding="utf-8") as f:
            if file.endswith(".json"):
                json.dump(data, f, indent=4)
            else:
                f.write(data)
        return True
    except Exception as e:
        raise ValueError(f"Error writing {file}: {e}")
