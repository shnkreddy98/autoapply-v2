import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Optional

from docx import Document

logger = logging.getLogger(__name__)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEX8 = re.compile(r"^[0-9A-Fa-f]{8}$")
# Color-bearing attributes that may carry a bad 8-digit value.
_COLOR_ATTRS = (_W + "color", _W + "fill", _W + "themeFill")


def normalize_docx_colors(docx_path: str) -> int:
    """Rewrite 8-digit ARGB color values (e.g. a border color of 'FF000000') to 6-digit
    RRGGBB by dropping the leading alpha byte. Some Word builds emit ARGB; LibreOffice
    can't parse it and silently drops the border/shading (seen as missing section
    underlines under e.g. Summary / Work Experience). Returns the number of attributes
    fixed. Best-effort: callers ignore failures and convert the file as-is."""
    doc = Document(docx_path)
    fixed = 0
    for el in doc.element.iter():
        for attr in _COLOR_ATTRS:
            v = el.get(attr)
            if v and _HEX8.match(v):
                el.set(attr, v[2:])
                fixed += 1
    if fixed:
        doc.save(docx_path)
    return fixed


async def convert_docx_to_pdf(resume_docx: str) -> Optional[str]:
    """Convert a .docx file to PDF using LibreOffice (headless). Returns the PDF path or None."""
    docx_path = Path(resume_docx).resolve()

    if not docx_path.exists():
        logger.error(f"DOCX file not found: {docx_path}")
        return None

    try:
        fixed = normalize_docx_colors(str(docx_path))
        if fixed:
            logger.info("normalized %d ARGB color attr(s) in %s", fixed, docx_path.name)
    except Exception as exc:  # never block conversion on the cleanup
        logger.warning("color normalization skipped for %s: %s", docx_path.name, exc)

    output_dir = str(docx_path.parent)
    expected_pdf = str(docx_path.with_suffix(".pdf"))

    try:
        process = await asyncio.create_subprocess_exec(
            "libreoffice",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            output_dir,
            str(docx_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            if os.path.exists(expected_pdf):
                logger.debug(f"Converted to PDF: {expected_pdf}")
                return expected_pdf
            else:
                logger.error(f"Conversion succeeded but PDF not found: {expected_pdf}")
                return None
        else:
            logger.error(f"LibreOffice failed: {stderr.decode().strip()}")
            return None

    except FileNotFoundError:
        logger.error("LibreOffice not found — make sure it is installed")
        return None
    except Exception as e:
        logger.error(f"PDF conversion error: {e}", exc_info=True)
        return None
