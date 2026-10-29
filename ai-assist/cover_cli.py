"""stdin/stdout bridge for `autoapply cover`, the same way answer_cli.py serves
`autoapply ask` (the auto-browse CLI cannot import ai-assist).

    echo '{"user_id":..., "job_id":..., "max_words":350, "label":null}' \
      | uv run python cover_cli.py

Prints {"text", "word_count", "base_label", "pdf_key", "txt_key"} on success,
{"error":...} with a non-zero exit otherwise.
"""

from __future__ import annotations

import asyncio
import json
import sys

import cover
import qa


async def _main() -> int:
    try:
        req = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"bad request json: {exc}"}))
        return 2

    from common import db
    from common import storage as storagemod

    storage = storagemod.from_env()
    try:
        with db.connect() as conn:
            ctx = cover.load(conn, storage, req["user_id"], req["job_id"], req.get("label"))
    except (qa.NotOwner, qa.MissingResume, qa.MissingJD) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    try:
        out = await cover.write_and_store(
            storage, ctx, req.get("max_words", cover.MAX_WORDS_DEFAULT)
        )
    except Exception as exc:  # LLM/network/LibreOffice — the CLI prints this verbatim
        print(json.dumps({"error": str(exc)}))
        return 1

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
