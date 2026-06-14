import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


async def convert_docx_to_pdf(resume_docx: str) -> Optional[str]:
    """Convert a .docx file to PDF using LibreOffice (headless). Returns the PDF path or None."""
    docx_path = Path(resume_docx).resolve()

    if not docx_path.exists():
        logger.error(f"DOCX file not found: {docx_path}")
        return None

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
