"""stdin/stdout bridge for `autoapply ask`.

The auto-browse CLI cannot import ai-assist (no build-system, and env.py loads
dev.env relative to cwd), so it shells out here the same way it does for the
tailor worker. Reading the question from stdin rather than argv keeps it out of
the process table and off the shell history.

    echo '{"user_id":..., "job_id":..., "question":..., "max_words":100}' \
      | uv run python answer_cli.py

Prints {"answer":..., "word_count":...} on success, {"error":...} with a
non-zero exit otherwise.
"""

from __future__ import annotations

import asyncio
import json
import sys

import qa


async def _main() -> int:
    try:
        req = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"bad request json: {exc}"}))
        return 2

    from common import db
    from common import storage as storagemod
    from textfmt import word_count

    try:
        with db.connect() as conn:
            resume, jd = qa.load_context(
                conn, storagemod.from_env(), req["user_id"], req["job_id"]
            )
    except (qa.NotOwner, qa.MissingResume, qa.MissingJD) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1

    try:
        text = await qa.run_agent(
            resume, jd, req["question"], req.get("max_words", qa.MAX_WORDS_DEFAULT)
        )
    except Exception as exc:  # LLM/network/parse — the CLI prints this verbatim
        print(json.dumps({"error": str(exc)}))
        return 1

    print(json.dumps({"answer": text, "word_count": word_count(text)}))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
